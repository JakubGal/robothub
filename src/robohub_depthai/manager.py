import logging as log
import time
from threading import Thread
from typing import List

import robothub
from robothub import RobotHubApplication

from robohub_depthai.hub_camera import HubCamera


class HubCameraManager:
    REPORT_FREQUENCY = 10  # seconds
    POLL_FREQUENCY = 0.05

    def __init__(self, app: RobotHubApplication, devices: List[dict]):
        self.hub_cameras = [HubCamera(app, device['serialNumber']) for device in devices]
        self.app = app

        self.reporting_thread = Thread(target=self._report, name='ReportingThread', daemon=False)
        self.polling_thread = Thread(target=self._poll, name='PollingThread', daemon=False)

    def __exit__(self):
        self.stop()

    def start(self):
        log.debug('Starting cameras...')
        for camera in self.hub_cameras:
            camera.start()

        log.debug('Starting reporting thread...')
        self.reporting_thread.start()
        log.debug('Reporting thread started successfully')

        log.debug('Starting polling thread...')
        self.polling_thread.start()
        log.debug('Polling thread started successfully')

        log.debug('Cameras started successfully')

    def stop(self):
        log.debug('Gracefully stopping threads...')
        self.app.stop_event.set()

        try:
            while self.reporting_thread.is_alive():
                time.sleep(1)
        except BaseException as e:
            log.error(f'Error while stopping reporting thread: {e}. Joining thread...')
            self.reporting_thread.join()

        try:
            while self.polling_thread.is_alive():
                time.sleep(1)
        except BaseException as e:
            log.error(f'Error while stopping polling thread: {e}. Joining thread...')
            self.polling_thread.join()

        try:
            robothub.AGENT.shutdown()
        except BaseException as e:
            log.debug(f'Agent shutdown excepted with {e}')

        try:
            robothub.STREAMS.destroy_all_streams()
        except BaseException as e:
            raise Exception(f'Destroy all streams excepted with: {e}')

        for camera in self.hub_cameras:
            try:
                if camera.device.state != robothub.DeviceState.DISCONNECTED:
                    camera.oak_camera.__exit__(Exception, 'Device disconnected - app shutting down', None)
            except BaseException as e:
                raise Exception(f'Could not exit device with error: {e}')
            robothub.STREAMS.destroy_all_streams()

        log.debug('App stopped successfully')

    def _report(self):
        while not self.app.stop_event.is_set():
            for camera in self.hub_cameras:
                device_info = camera.oak_camera.get_info_report()
                device_stats = camera.oak_camera.get_stats_report()

                self.app.agent.publish_device_info(device_info)
                self.app.agent.publish_device_stats(device_stats)

            time.sleep(self.REPORT_FREQUENCY)

    def _poll(self):
        while self.app.stop_event.is_set():
            for camera in self.hub_cameras:
                camera.poll()

            time.sleep(self.POLL_FREQUENCY)
