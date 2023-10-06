import datetime
import itertools
import logging
import tempfile
import uuid
from collections import deque
from pathlib import Path
from queue import Queue, Empty

from depthai_sdk.recorders.video_writers import AvWriter

try:
    import av
except ImportError:
    av = None

logger = logging.getLogger(__name__)


class FrameBuffer:
    def __init__(self, maxlen: int = None):
        self.buffer = deque(maxlen=maxlen)
        self.temporary_queues = {}

    def get_slice(self, start: int, end: int = None) -> list:
        """
        Get a slice of the buffer.

        :param start: Start index.
        :param end: End index. If None, return all elements from `start` to the end of the buffer.
        :return: Slice of the buffer.
        """
        return list(
            itertools.islice(self.buffer, int(start), end)
        )

    def save_video(self,
                   before_seconds: int,
                   after_seconds: int,
                   fps: int,
                   frame_width: int,
                   frame_height: int,
                   return_bytes=False
                   ) -> str | bytes | None:
        """
        Save a video of the last `before_seconds` seconds and the next `after_seconds` seconds.

        :param before_seconds: Number of seconds to save before the current time.
        :param after_seconds: Number of seconds to save after the current time.
        :param fps: The FPS of the video.
        :param frame_width: Video frame width.
        :param frame_height: Video frame height.
        :param return_bytes: If True, return the video as bytes. Otherwise, save the video to disk and return the path.
        :return:
        """
        if not av:
            raise ImportError('av library is not installed. Cannot save video. '
                              'Please make sure PyAV is installed (`pip install pyav`).')

        if before_seconds < 0 or after_seconds < 0:
            raise ValueError('`before_seconds` and `after_seconds` must be positive.')

        video_frames_before = self.get_slice(start=self.buffer.maxlen - before_seconds * fps)
        video_frames_after = []
        temp_queue = Queue()
        queue_uuid = uuid.uuid4()
        self.temporary_queues[queue_uuid] = temp_queue
        latest_t_before = video_frames_before[-1].msg.getTimestamp()

        def wait_until_complete():
            while True:
                try:
                    p = self.temporary_queues[queue_uuid].get(block=True)
                    timestamp = p.msg.getTimestamp()
                    if timestamp > latest_t_before:
                        video_frames_after.append(p)
                    if timestamp - latest_t_before > datetime.timedelta(seconds=after_seconds):
                        break

                except Empty:
                    # robothub_core.wait(1 / fps)
                    pass

        wait_until_complete()

        self.temporary_queues.pop(queue_uuid)
        return self._mux_video(packets=video_frames_before + video_frames_after,
                               fps=fps,
                               frame_width=frame_width,
                               frame_height=frame_height,
                               return_bytes=return_bytes)

    def _mux_video(self,
                   packets: list,
                   fps: int,
                   frame_width: int,
                   frame_height: int,
                   return_bytes: bool = False
                   ) -> str | bytes | None:
        """
        Mux a list of packets into a video.

        :param packets: List of DepthAI packets.
        :param fps: The FPS of the video.
        :param frame_width: Video frame width.
        :param frame_height: Video frame height.
        :param return_bytes: If True, return the video as bytes. Otherwise, save the video to disk and return the path.
        :return:
        """
        with tempfile.TemporaryDirectory() as dir_path:
            name = str(uuid.uuid4())
            av_writer = AvWriter(path=Path(dir_path),
                                 name=name,
                                 fourcc='h264',
                                 fps=fps,
                                 frame_shape=(frame_width, frame_height))

            for p in packets:
                av_writer.write(p.msg)

            av_writer.close()

            video_path = Path(dir_path, name).with_suffix('.mp4')
            if not return_bytes:
                return str(video_path)

            with open(video_path, 'rb') as f:
                return f.read()

    def default_callback(self, packet) -> None:
        """
        Default callback for the frame buffer. It will append the packet to the buffer and put it in all temporary queues. 
        """
        self.buffer.append(packet)
        for _, queue in self.temporary_queues.items():
            queue.put(packet)

    @property
    def maxlen(self) -> int:
        return self.buffer.maxlen
