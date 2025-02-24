import logging
import threading
import time
from typing import List, Optional, Union, Dict, Tuple

import depthai as dai
import numpy as np
import robothub_core
from depthai_sdk import OakCamera
from depthai_sdk.components import Component, CameraComponent, StereoComponent, NNComponent
from depthai_sdk.oak_outputs.xout.xout_base import StreamXout
from depthai_sdk.oak_outputs.xout.xout_h26x import XoutH26x
from depthai_sdk.visualize.objects import VisText, VisLine

from robothub.events import send_video_event
from robothub.frame_buffer import FrameBuffer
from robothub.live_view_utils import create_stream_handle
from robothub.types import BoundingBox
from robothub.live_view_utils import is_h264_frame

__all__ = ['LiveView', 'LIVE_VIEWS']

logger = logging.getLogger(__name__)


def _publish_data(stream_handle: robothub_core.StreamHandle,
                  h264_frame,
                  rectangles: List[BoundingBox],
                  rectangle_labels: List[str],
                  texts: List[VisText],
                  lines: List[VisLine],
                  frame_width: int,
                  frame_height: int
                  ) -> None:
    """
    Publish data to a stream.

    :param stream_handle: robothub_core.StreamHandle object to publish to.
    :param h264_frame: H264 frame to publish.
    :param rectangles: List of bounding boxes to publish.
    :param rectangle_labels: List of labels for the bounding boxes.
    :param texts: List of texts to publish.
    :param lines: List of lines to publish.
    :param frame_width: Width of the frame.
    :param frame_height: Height of the frame.
    """
    timestamp = int(time.perf_counter_ns() / 1e6)
    metadata = {
        "platform": "robothub",
        "frame_shape": [frame_height, frame_width],
        "config": {
            "output": {
                "img_scale": 1.0,
                "show_fps": False,
                "clickable": True
            },
            "detection": {
                "thickness": 1,
                "fill_transparency": 0.05,
                "box_roundness": 0,
                "color": [0, 255, 0],
                "bbox_style": 0,
                "line_width": 0.5,
                "line_height": 0.5,
                "hide_label": False,
                "label_position": 0,
                "label_padding": 10
            },
            'text': {
                'font_color': [255, 255, 0],
                'font_transparency': 0.5,
                'font_scale': 1.0,
                'font_thickness': 2,
                'bg_transparency': 0.5,
                'bg_color': [0, 0, 0]
            }
        },
        "objects": [
            {
                "type": "detections",
                "detections": []
            }
        ]
    }

    # Bounding boxes
    for roi, label in zip(rectangles, rectangle_labels):
        xmin, ymin, xmax, ymax = roi
        metadata['objects'][0]['detections'].append(
            {'bbox': [xmin, ymin, xmax, ymax], 'label': label, 'color': [0, 255, 255]}
        )

    # Texts
    for text in texts:
        metadata["objects"].append(text.prepare().serialize())

    # Lines
    for line in lines:
        metadata["objects"].append(line.prepare().serialize())

    # Publish
    stream_handle.publish_video_data(bytes(h264_frame), timestamp, metadata)


class LiveView:
    def __init__(self,
                 name: str,
                 unique_key: str,
                 device_mxid: str,
                 frame_width: int,
                 frame_height: int,
                 fps: int,
                 max_buffer_size: int):
        """
        Class for creating and publishing Live Views.

        :param name: Name of the Live View.
        :param unique_key: Live View identifier.
        :param device_mxid: MXID of the device that is streaming the Live View.
        :param frame_width: Frame width.
        :param frame_height: Frame height.
        :param max_buffer_size: Maximum number of seconds to buffer.
        """
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.device_mxid = device_mxid
        self.unique_key = unique_key
        self.name = name
        self.fps = fps

        self.stream_handle = create_stream_handle(camera_serial=device_mxid, unique_key=unique_key, name=name)
        self.__validated_frame_h264 = False

        # Objects
        self.texts: List[VisText] = []
        self.rectangles: List[BoundingBox] = []
        self.labels: List[str] = []
        self.lines: List[VisLine] = []

        self.frame_buffer = FrameBuffer(maxlen=int(max_buffer_size * fps))

    @staticmethod
    def create(device: OakCamera,
               component: Union[CameraComponent, StereoComponent],
               name: str,
               unique_key: str = None,
               manual_publish: bool = False,
               max_buffer_size: int = 0,
               ) -> 'LiveView':
        """
        Creates a Live View for a given component.

        :param device: OakCamera instance.
        :param component: Component to create a Live View for. Either a CameraComponent, StereoComponent or NNComponent.
        :param name: Name of the Live View.
        :param unique_key: Live View identifier.
        :param manual_publish: If True, the Live View will not be automatically published. Use LiveView.publish() to publish the Live View.
        :param max_buffer_size: Maximum number of seconds to buffer.
        """
        output = None
        is_h264 = LiveView._is_encoder_enabled(component)
        if not is_h264:
            output = LiveView._h264_output(device, component)
        elif not is_h264 and not isinstance(component, CameraComponent):
            raise ValueError(f'Component {component.__class__.__name__} must have h264 encoding '
                             f'enabled to be used with LiveView.')

        w, h = LiveView._get_stream_size(component)
        fps = LiveView._get_component_fps(component)
        device_mxid = device.device.getMxId()
        unique_key = unique_key or f'{device_mxid}_{component.__class__.__name__.lower()}_encoded'

        live_view = LiveView(name=name,
                             unique_key=unique_key,
                             device_mxid=device_mxid,
                             frame_width=w,
                             frame_height=h,
                             fps=fps,
                             max_buffer_size=max_buffer_size)

        if not manual_publish:
            device.callback(output or component.out.encoded, live_view._publish_callback)
        else:
            device.callback(output or component.out.encoded, live_view.frame_buffer.default_callback)

        LIVE_VIEWS[unique_key] = live_view
        return live_view

    @staticmethod
    def _h264_output(device: OakCamera, component: CameraComponent):
        """
        Creates an h264 output for a given component.

        :param device: OakCamera instance.
        :param component: Component to create an h264 output for.
        :return: DepthAI SDK output.
        """
        fps = LiveView._get_component_fps(component)
        encoder = device.pipeline.createVideoEncoder()
        encoder_profile = dai.VideoEncoderProperties.Profile.H264_MAIN
        encoder.setDefaultProfilePreset(fps, encoder_profile)
        encoder.input.setQueueSize(1)
        encoder.input.setBlocking(False)
        encoder.setKeyframeFrequency(int(fps))
        encoder.setBitrate(1500 * 1000)
        encoder.setRateControlMode(dai.VideoEncoderProperties.RateControlMode.CBR)
        encoder.setNumFramesPool(3)

        component.node.video.link(encoder.input)

        def encoded(pipeline, device):
            xout = XoutH26x(
                frames=StreamXout(encoder.id, encoder.bitstream),
                color=True,
                profile=encoder_profile,
                fps=encoder.getFrameRate(),
                frame_shape=component.node.getResolution()
            )
            xout.name = f'{component._source}_h264'
            return component._create_xout(pipeline, xout)

        return encoded

    @staticmethod
    def _get_component_fps(component) -> int | float:
        """
        Gets the FPS of a component.

        :param component: DepthAI SDK component.
        :return: FPS of the component.
        """
        if isinstance(component, StereoComponent):
            return component._fps
        elif isinstance(component, CameraComponent):
            return component.get_fps()

        return 30

    @staticmethod
    def _is_encoder_enabled(component: Component) -> bool:
        """
        Checks if the component has h264 encoding enabled.

        :param component: Component to check.
        :return: True if the component has h264 encoding enabled, False otherwise.
        :raises ValueError: If the component is not a CameraComponent or StereoComponent.
        """
        if not isinstance(component, CameraComponent) and not isinstance(component, StereoComponent):
            raise ValueError(f'Component {component.__class__.__name__} must be a CameraComponent or StereoComponent.')
        encoder = component.encoder

        if encoder and encoder.getProfile() != dai.VideoEncoderProperties.Profile.H264_MAIN:
            return False

        return True

    @staticmethod
    def _get_stream_size(component) -> Tuple[int, int]:
        """
        Internal method to get the stream size of a component.

        :param component: DepthAI SDK component to get the stream size of.
        """
        if isinstance(component, CameraComponent):
            return component.stream_size
        elif isinstance(component, StereoComponent):
            return component.left.stream_size
        elif isinstance(component, NNComponent):
            return component._input.stream_size

    @staticmethod
    def get(unique_key: str = None, name: str = None) -> Optional['LiveView']:
        """
        Gets a Live View by its unique key or name. Name takes precedence over unique key.

        :param unique_key: Unique key of the Live View.
        :param name: Name of the Live View.
        :return: Live View with the given unique key or name.
        :raises ValueError: If neither name nor unique_key is specified.
        """
        if name is not None:
            return LiveView.get_by_name(name)
        elif unique_key is not None:
            return LiveView.get_by_unique_key(unique_key)
        else:
            raise ValueError('Either name or unique_key must be specified.')

    @staticmethod
    def get_by_name(name: str) -> Optional['LiveView']:
        """
        Gets a Live View by its name.

        :param name: Name of the Live View.
        :return: Live View with the given name. None if a Live View with the given name does not exist.
        """
        for live_view in LIVE_VIEWS.values():
            if live_view.name == name:
                return live_view
        return None

    @staticmethod
    def get_by_unique_key(unique_key: str) -> Optional['LiveView']:
        """
        Gets a Live View by its unique key.

        :param unique_key: Unique key of the Live View.
        :return: Live View with the given unique key.
        :raises ValueError: If a Live View with the given unique key does not exist.
        """
        if unique_key not in LIVE_VIEWS:
            raise ValueError(f'Live View with unique_key {unique_key} does not exist.')

        return LIVE_VIEWS[unique_key]

    def save_video_event(self,
                         before_seconds: int,
                         after_seconds: int,
                         title: str,
                         ) -> None:
        """
        Saves a video event to the frame buffer, then calls `on_complete` when the video is ready.
        When the video is ready, the `on_complete` function will be called with the path to the video file.
        Note: When app is stopped, it is not guaranteed that the video will be saved.

        :param before_seconds: Number of seconds to save before the event occurred.
        :param after_seconds: Number of seconds to save after the event occurred.
        :param title: Title of the video event.
        """

        if self.frame_buffer.maxlen == 0:
            raise Exception('You have set `max_buffer_size` to zero, therefore you cannot use frame buffer.')

        # We need to start a new thread because we cannot block the main thread.
        def on_complete(video_path):
            send_video_event(video_path, title)

        kwargs = {
            'before_seconds': before_seconds,
            'after_seconds': after_seconds,
            'fps': self.fps,
            'frame_width': self.frame_width,
            'frame_height': self.frame_height,
            'on_complete': on_complete,
            'delete_after_complete': True
        }
        t = threading.Thread(target=self.frame_buffer.save_video,
                             kwargs=kwargs,
                             daemon=True)
        t.start()

    def add_rectangle(self, rectangle: BoundingBox, label: str) -> None:
        """
        Adds a rectangle (bounding box) to the live view.

        :param rectangle: Tuple (x1, y1, x2, y2) where (x1, y1) is the top left corner and (x2, y2) is the bottom right corner.
        :param label: Label to display on the rectangle.
        """
        self.rectangles.append(rectangle)
        self.labels.append(label)

    def add_text(self,
                 text: str,
                 coords: Tuple[int, int],
                 size: int = None,
                 color: Tuple[int, int, int] = None,
                 thickness: int = None,
                 outline: bool = True,
                 background_color: Tuple[int, int, int] = None,
                 background_transparency: float = 0.5
                 ) -> None:
        """
        Adds text to the live view.

        :param text: Text to display.
        :param coords: Tuple (x, y) where (x, y) is the top left corner of the text.
        :param size: Size of the text.
        :param color: Color of the text. E.g., (255, 0, 0) for red.
        :param thickness: Thickness of the text.
        :param outline: True to display an outline around the text, False otherwise.
        :param background_color: Color of the background. E.g., (0, 0, 0) for black.
        :param background_transparency: Transparency of the background. 0.0 is fully transparent, 1.0 is fully opaque.
        """
        obj = VisText(text, coords, size, color, thickness, outline, background_color, background_transparency)
        self.texts.append(obj)

    def add_line(self,
                 pt1: Tuple[int, int],
                 pt2: Tuple[int, int],
                 color: Tuple[int, int, int] = None,
                 thickness: int = None
                 ) -> None:
        """
        Adds a line to the live view.

        :param pt1: (x, y) coordinates of the start point of the line.
        :param pt2: (x, y) coordinates of the end point of the line.
        :param color: Color of the line. E.g., (255, 0, 0) for red.
        :param thickness: Thickness of the line.
        """
        obj = VisLine(pt1, pt2, color, thickness)
        self.lines.append(obj)

    def publish(self, h264_frame: Union[np.array, List]) -> None:
        """
        Publishes a frame to the live view. If manual_publish is set to True,
        this function must be called to publish a frame. Otherwise, the frame will automatically be published.

        :param h264_frame: H264 frame to publish.
        """
        if not self.__validated_frame_h264:
            if not is_h264_frame(h264_frame):
                logger.error('Frame is not H.264 encoded, '
                             'please make sure the pipeline is configured correctly.\n'
                             'RobotHub supports H.264 encoded frames only.')
            self.__validated_frame_h264 = True

        _publish_data(stream_handle=self.stream_handle,
                      h264_frame=h264_frame,
                      rectangles=self.rectangles,
                      rectangle_labels=self.labels,
                      texts=self.texts,
                      lines=self.lines,
                      frame_width=self.frame_width,
                      frame_height=self.frame_height)
        self._reset_overlays()

    def _publish_callback(self, h264_packet) -> None:
        """
        Default callback for publishing a frame. This method also calls the default callback of the frame buffer,
        which is used to save video events.

        :param h264_packet: H264 packet to publish.
        """
        self.publish(h264_frame=h264_packet.frame)
        self.frame_buffer.default_callback(h264_packet)

    def _reset_overlays(self) -> None:
        """
        Resets the overlays. This is called after a frame is published.
        """
        self.rectangles.clear()
        self.labels.clear()
        self.lines.clear()
        self.texts.clear()


LIVE_VIEWS: Dict[str, LiveView] = dict()
