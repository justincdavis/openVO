from threading import Thread, Lock
from typing import Dict, Tuple, Optional

import depthai as dai
import numpy as np
import cv2


class OAK_Camera:
    def __init__(
        self,
        rgb_size: Tuple[int, int] = (1920, 1080),
        mono_size: Tuple[int, int] = (1280, 720),
        extended_disparity: bool = True,
        subpixel: bool = True,
        lr_check: bool = True,
    ):
        self._rgb_width = rgb_size[0]
        self._rgb_height = rgb_size[1]
        self._left_width = mono_size[0]
        self._right_width = self._left_width
        self._left_height = mono_size[1]
        self._right_height = self._left_height

        self._extended_disparity = extended_disparity
        self._subpixel = subpixel
        self._lr_check = lr_check

        with dai.Device() as device:
            calibData = device.readCalibration()

            self._M_rgb = np.array(
                calibData.getCameraIntrinsics(
                    dai.CameraBoardSocket.RGB, self._right_width, self._rgb_height
                )
            )
            self._M_left = np.array(
                calibData.getCameraIntrinsics(
                    dai.CameraBoardSocket.LEFT, self._left_width, self._left_height
                )
            )
            self._M_right = np.array(
                calibData.getCameraIntrinsics(
                    dai.CameraBoardSocket.RIGHT, self._right_width, self._right_height
                )
            )
            self._D_left = np.array(
                calibData.getDistortionCoefficients(dai.CameraBoardSocket.LEFT)
            )
            self._D_right = np.array(
                calibData.getDistortionCoefficients(dai.CameraBoardSocket.RIGHT)
            )
            self._rgb_fov = calibData.getFov(dai.CameraBoardSocket.RGB)
            self._mono_fov = calibData.getFov(dai.CameraBoardSocket.LEFT)

            self._R1 = np.array(calibData.getStereoLeftRectificationRotation())
            self._R2 = np.array(calibData.getStereoRightRectificationRotation())

            self._H_left = np.matmul(
                np.matmul(self._M_right, self._R1), np.linalg.inv(self._M_left)
            )
            self._H_right = np.matmul(
                np.matmul(self._M_right, self._R1), np.linalg.inv(self._M_right)
            )

            self._baseline = calibData.getBaselineDistance()  # in centimeters

        self._focal_length = self._M_left[0, 0]
        self._cx, self._cy = self._M_left[0, 2], self._M_left[1, 2]
        self._Q = np.float32(
            [
                [1, 0, 0, -self._cx],
                [0, 1, 0, -self._cy],
                [0, 0, 0, self._focal_length],
                [0, 0, -1.0 / self._baseline, 0],
            ]
        )

        # pipeline
        self._pipeline: dai.Pipeline = dai.Pipeline()
        # storage for the nodes
        self._nodes: Dict[str, Tuple[dai.Node, dai.XLinkOut]] = {}
        # stop condition
        self._stopped: bool = False
        # thread for the camera
        self._cam_thread = Thread(target=self._target)
        self._data_lock = Lock()

        self._rgb_frame: Optional[np.ndarray] = None
        self._depth_frame: Optional[np.ndarray] = None
        self._left_frame: Optional[np.ndarray] = None
        self._right_frame: Optional[np.ndarray] = None
        self._rectified_left_frame: Optional[np.ndarray] = None
        self._rectified_right_frame: Optional[np.ndarray] = None

        # display information
        self._display_thread = Thread(target=self._display)
        self._display_stopped = False

    @property
    def rgb_frame(self) -> Optional[np.ndarray]:
        """
        Get the rgb color frame
        """
        return self._rgb_frame

    @property
    def disparity(self) -> Optional[np.ndarray]:
        """
        Gets the disparity frame
        """
        return self._depth_frame

    @property
    def left_frame(self) -> Optional[np.ndarray]:
        """
        Gets the left frame
        """
        return self._left_frame

    @property
    def right_frame(self) -> Optional[np.ndarray]:
        """
        Gets the right frame
        """
        return self._right_frame

    @property
    def rectified_left_frame(self) -> Optional[np.ndarray]:
        """
        Gets the rectified left frame
        """
        return self._rectified_left_frame

    @property
    def rectified_right_frame(self) -> Optional[np.ndarray]:
        """
        Gets the rectified right frame
        """
        return self._rectified_right_frame

    def start(self) -> None:
        """
        Starts the camera
        """
        self._cam_thread.start()

    def stop(self) -> None:
        """
        Stops the camera
        """
        self._stopped = True
        self._cam_thread.join()

    def _display(self) -> None:
        while not self._display_stopped:
            if self._rgb_frame is not None:
                cv2.imshow("rgb", self._rgb_frame)
            if self._depth_frame is not None:
                cv2.imshow("depth", self._depth_frame)
            if self._left_frame is not None:
                cv2.imshow("left", self._left_frame)
            if self._right_frame is not None:
                cv2.imshow("right", self._right_frame)
            if self._rectified_left_frame is not None:
                cv2.imshow("rectified left", self._rectified_left_frame)
            if self._rectified_right_frame is not None:
                cv2.imshow("rectified right", self._rectified_right_frame)
            cv2.waitKey(1)

    def start_display(self) -> None:
        """
        Starts the display thread
        """
        self._display_thread.start()

    def stop_display(self) -> None:
        """
        Stops the display thread
        """
        self._display_stopped = True
        self._display_thread.join()

    def _create_cam_rgb(self) -> None:
        cam_rgb = self._pipeline.create(dai.node.ColorCamera)
        xout_video = self._pipeline.create(dai.node.XLinkOut)
        xout_video.setStreamName("color_camera")
        cam_rgb.setBoardSocket(dai.CameraBoardSocket.RGB)
        if self._rgb_height == 1080:
            cam_rgb.setResolution(dai.ColorCameraProperties.SensorResolution.THE_1080_P)
        elif self._rgb_height == 2160:
            cam_rgb.setResolution(dai.ColorCameraProperties.SensorResolution.THE_4_K)
        else:
            raise NotImplementedError(
                f"Resolution not implemented: {self._rgb_width}, {self._rgb_height}"
            )

        cam_rgb.setVideoSize(self._rgb_width, self._rgb_height)
        xout_video.input.setBlocking(False)
        xout_video.input.setQueueSize(1)
        cam_rgb.video.link(xout_video.input)

        self._nodes["color_camera"] = (cam_rgb, xout_video)

    def _create_stereo(self) -> None:
        # Define sources and outputs
        mono_left = self._pipeline.create(dai.node.MonoCamera)
        mono_right = self._pipeline.create(dai.node.MonoCamera)
        depth = self._pipeline.create(dai.node.StereoDepth)

        xout_depth = self._pipeline.create(dai.node.XLinkOut)
        xout_depth.setStreamName("disparity")
        xout_rect_left = self._pipeline.create(dai.node.XLinkOut)
        xout_rect_left.setStreamName("rectified_left")
        xout_rect_right = self._pipeline.create(dai.node.XLinkOut)
        xout_rect_right.setStreamName("rectified_right")

        # Properties
        mono_left.setResolution(dai.MonoCameraProperties.SensorResolution.THE_720_P)
        mono_left.setBoardSocket(dai.CameraBoardSocket.LEFT)
        mono_right.setResolution(dai.MonoCameraProperties.SensorResolution.THE_720_P)
        mono_right.setBoardSocket(dai.CameraBoardSocket.RIGHT)

        # Create a node that will produce the depth map (using disparity output as it's easier to visualize depth this way)
        depth.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_DENSITY)
        # Options: MEDIAN_OFF, KERNEL_3x3, KERNEL_5x5, KERNEL_7x7 (default)
        depth.initialConfig.setMedianFilter(dai.MedianFilter.KERNEL_7x7)
        depth.setLeftRightCheck(self._lr_check)
        depth.setExtendedDisparity(self._extended_disparity)
        depth.setSubpixel(self._subpixel)

        config = depth.initialConfig.get()
        config.postProcessing.speckleFilter.enable = False
        config.postProcessing.speckleFilter.speckleRange = 50
        config.postProcessing.temporalFilter.enable = True
        config.postProcessing.spatialFilter.enable = True
        config.postProcessing.spatialFilter.holeFillingRadius = 2
        config.postProcessing.spatialFilter.numIterations = 1
        config.postProcessing.thresholdFilter.minRange = 400
        config.postProcessing.thresholdFilter.maxRange = 15000
        config.postProcessing.decimationFilter.decimationFactor = 1
        depth.initialConfig.set(config)

        # Linking
        mono_left.out.link(depth.left)
        mono_right.out.link(depth.right)
        depth.disparity.link(xout_depth.input)
        depth.rectifiedLeft.link(xout_rect_left.input)
        depth.rectifiedRight.link(xout_rect_right.input)

        self._nodes["stereo"] = (depth, xout_depth)
        self._nodes["mono_left"] = (mono_left, None)
        self._nodes["mono_right"] = (mono_right, None)
        self._nodes["rectified_left"] = (depth, xout_rect_left)
        self._nodes["rectified_right"] = (depth, xout_rect_right)

    def _target(self) -> None:
        self._create_cam_rgb()
        self._create_stereo()
        with dai.Device(self._pipeline) as device:
            queues = {}
            for key in self._nodes.keys():
                if self._nodes[key] is not None:
                    queues[key] = device.getOutputQueue(
                        name=key, maxSize=1, blocking=False
                    )

            # TODO: handle these concurrently
            while not self._stopped:
                with self._data_lock:  # ensures that disparity and left frame are updated together
                    for name, queue in queues.items():
                        if queue is not None:
                            data = queue.get()
                            if name == "color_camera":
                                self._color_frame = data.getCvFrame()
                            elif name == "disparity":
                                self._disparity = data.getCvFrame()
                            elif name == "rectified_left":
                                self._left_rect_frame = data.getCvFrame()
                            elif name == "rectified_right":
                                self._right_rect_frame = data.getCvFrame()
                            elif name == "mono_left":
                                self._left_frame = data.getCvFrame()
                            elif name == "mono_right":
                                self._right_frame = data.getCvFrame()

    def compute_3d(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute 3D point cloud from disparity map.
        Returns:
            Tuple[np.ndarray, np.ndarray, np.ndarray]: 3D point cloud, disparity map, left frame
        """
        with self._data_lock:  # ensures that disparity and left frame are updated together
            disparity = self._disparity
            left_frame = self._rectified_left_frame
            if disparity is None or left_frame is None:
                return None, None, None
        img3d = cv2.reprojectImageTo3D(disparity, self._Q)
        return img3d, disparity, left_frame
