import contextlib
import logging as log
import os

import robothub

__all__ = ['HubCameraManager']


class NoDevicesException(Exception):
    pass


class HubCameraManager:
    """
    A manager class to handle multiple HubCamera instances.
    """
    REPORT_FREQUENCY = 10  # seconds
    POLL_FREQUENCY = 0.0005

    def __init__(self):
        self._devices = []
        self._connected_devices = []

        self.running = False
        self.stop_event = robothub.threading.Event()

        self.lock = robothub.threading.Lock()
        self.reporting_thread = robothub.threading.Thread(target=self._report, name='ReportingThread', daemon=False)
        self.polling_thread = robothub.threading.Thread(target=self._poll, name='PollingThread', daemon=False)
        self.connection_thread = robothub.threading.Thread(target=self._connect, name='ConnectionThread', daemon=False)

    def __exit__(self):
        self.stop()

    def start(self) -> None:
        """
        Start the cameras, start reporting and polling threads.
        """
        self.running = True

        log.info('Device connection thread: starting...')
        self.connection_thread.start()
        log.info('Device connection thread: started successfully.')

        # Endless loop to prevent app from exiting if no devices are found
        while self.running:
            if self._devices:
                break

            self.stop_event.wait(5)

        log.info('Devices: starting...')
        for device in self._devices:
            device.start()
            self._connected_devices.append(device)

        log.info('Reporting thread: starting...')
        self.reporting_thread.start()
        log.info('Reporting thread: started successfully.')

        log.info('Polling thread: starting...')
        self.polling_thread.start()
        log.info('Polling thread: started successfully.')

        log.info('Devices: started successfully.')

    def stop(self) -> None:
        """
        Stop the cameras, stop reporting and polling threads.
        """
        log.debug('Threads: gracefully stopping...')
        self.stop_event.set()

        self.__graceful_thread_join(self.connection_thread)
        self.__graceful_thread_join(self.reporting_thread)
        self.__graceful_thread_join(self.polling_thread)

        try:
            robothub.STREAMS.destroy_all_streams()
        except BaseException as e:
            raise Exception(f'Destroy all streams excepted with: {e}.')

        for camera in self.booted_cameras:
            try:
                if camera.state != robothub.DeviceState.DISCONNECTED:
                    with open(os.devnull, 'w') as devnull:
                        with contextlib.redirect_stdout(devnull):
                            camera.hub_camera.__exit__(Exception, 'Device disconnected - app shutting down', None)
            except BaseException as e:
                raise Exception(f'Device {camera.device_mxid}: could not exit with exception: {e}.')

        log.info('App: stopped successfully.')

    def add_device(self, device: 'Device') -> None:
        """
        Add a camera to the list of cameras.
        """
        self._devices.append(device)

    def remove_device(self, device: 'Device') -> None:
        """
        Remove a camera from the list of cameras.
        """
        self._devices.remove(device)

    def _report(self) -> None:
        """
        Reports the state of the cameras to the agent. Active when app is running, inactive when app is stopped.
        Reporting frequency is defined by REPORT_FREQUENCY.
        """
        while self.running:
            for camera in self.booted_cameras:
                try:
                    device_info = camera.info_report()
                    device_stats = camera.stats_report()

                    robothub.AGENT.publish_device_info(device_info)
                    robothub.AGENT.publish_device_stats(device_stats)
                except Exception as e:
                    log.debug(f'Device {camera.device_mxid}: could not report info/stats with error: {e}.')

            self.stop_event.wait(self.REPORT_FREQUENCY)

    def _poll(self) -> None:
        """
        Polls the cameras for new detections. Polling frequency is defined by POLL_FREQUENCY.
        """
        while self.running:
            for camera in self.booted_cameras:
                if not camera.poll():
                    log.info(f'Device {camera.device_mxid}: disconnected.')
                    camera.hub_camera = None
                    continue

            self.stop_event.wait(self.POLL_FREQUENCY)

    def _connect(self) -> None:
        """
        Reconnects the cameras that were disconnected or reconnected.
        """
        while self.running:
            if len(self._connected_devices) == len(self._devices):
                self.stop_event.wait(5)
                continue

            # self._update_hub_cameras(devices=self.devices)
            # TODO

    @staticmethod
    def __graceful_thread_join(thread) -> None:
        """
        Gracefully stop a thread.
        """
        try:
            if thread.is_alive():
                thread.join()
        except BaseException as e:
            log.error(f'{thread.getName()}: join excepted with: {e}.')
