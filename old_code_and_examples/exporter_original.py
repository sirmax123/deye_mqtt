#!/usr/bin/env python3

import abc
import argparse
import collections.abc
from concurrent import futures
import itertools
import logging
import os
import threading
from typing import Iterable

import prometheus_client
import random
import sys
import time

from prometheus_client import Metric

APP_NAME = 'example-exporter'
MIN_THREAD_COUNT = 2

log = logging.getLogger()


def main():
    parser = argparse.ArgumentParser(APP_NAME)
    parser.add_argument(
        '--debug', action='store_true', default=False,
        help='enable "debug" mode - more detailed logging')
    parser.add_argument(
        '--concurrency', type=int, default=os.cpu_count(),
        help='number of thread to use (min {})'.format(MIN_THREAD_COUNT))
    parser.add_argument(
        '--http-port', default=8080,
        help='port for http server to bind')

    args = parser.parse_args()

    init_logging(debug=args.debug)
    log.debug('program arguments: %s', args)

    prometheus_client.REGISTRY.unregister(prometheus_client.PROCESS_COLLECTOR)
    prometheus_client.REGISTRY.unregister(prometheus_client.PLATFORM_COLLECTOR)
    prometheus_client.REGISTRY.unregister(prometheus_client.GC_COLLECTOR)

    concurrency = max(MIN_THREAD_COUNT, args.concurrency)
    executor = futures.ThreadPoolExecutor(concurrency)

    monitor = Monitor(executor)
    exporter = Exporter(monitor)
    prometheus_client.REGISTRY.register(exporter)

    prometheus_client.start_http_server(args.http_port)
    while True:
        time.sleep(60)
        log.debug('sleep iteration')


def init_logging(debug=False):
    root = logging.getLogger()

    stderr_handler = logging.StreamHandler(stream=sys.stdout)
    stderr_handler.setFormatter(logging.Formatter(
        '## %(asctime)s %(threadName)s %(message)s'))
    root.addHandler(stderr_handler)

    if debug:
        root.setLevel(logging.DEBUG)
    else:
        root.setLevel(logging.INFO)


class Exporter(prometheus_client.registry.Collector):
    def __init__(self, monitor):
        self._monitor = monitor

    def collect(self) -> Iterable[Metric]:
        log.debug('Start data scraping')
        report = Report()
        try:
            self._monitor.iteration(report)
        except ValueError as e:
            log.error('Scrapping error: {}', e)
            report.scraping_postpone.inc()
            # return [self.scrapping_postpone]
            return report.error_view()
        finally:
            log.debug('Done data scraping')
        return report


class _Counter:
    _overflow_at = 0xffffffff

    def __init__(self):
        self.value = 0

    def inc(self):
        self.add(1)

    def add(self, value):
        if value < 0:
            raise ValueError(
                f'{type(self).__name__}.add() do not support negative values '
                f'(value=={value})')
        self.value += value
        if self._overflow_at < self.value:
            value -= self._overflow_at


class Report(collections.abc.Iterable):
    scraping_errors = _Counter()  # app globals
    scraping_postpone = _Counter()  # app globals

    def __init__(self):
        self.dummy_metric = prometheus_client.Gauge(
            'dummy_metric',
            'Some imaginable metric required only for demonstration',
            labelnames=('name',), registry=None)

    def __iter__(self):
        return itertools.chain(
            self._make_error_metrics(),
            self.dummy_metric.collect())

    def error_view(self):
        return self._make_error_metrics()

    def _make_error_metrics(self):
        return [
            prometheus_client.metrics_core.CounterMetricFamily(
                'scraping_errors', 'Count error happens during scrapping data',
                self.scraping_errors.value),
            prometheus_client.metrics_core.CounterMetricFamily(
                'scraping_postpone', 'Count of postponed scrapping requests',
                self.scraping_postpone.value)]


class Monitor:
    _lock = threading.Lock()

    def __init__(self, executor):
        self._executor = executor
        self._futures = []

    def iteration(self, report):
        if not self._lock.acquire(blocking=False):
            # do not allow to start parallels execution
            # it can work into multirequest mode, but better not dp it
            raise ValueError('Busy')
        try:
            self._iteration(report)
        finally:
            self._lock.release()

    def _iteration(self, report):
        log.info('Start monitor iteration')

        self._produce_commands()

        log.debug('Wait for tasks in threads to complete')

        pending = self._swap_futures()
        while pending:
            log.debug('pending tasks: %s', pending)
            done, incomplete = futures.wait(
                pending, return_when=futures.FIRST_COMPLETED)
            for f in done:
                try:
                    command = f.result()
                except Exception:
                    log.error('Error during processing of %s', f, exc_info=True)
                    report.scraping_errors.inc()
                    continue
                log.debug('Command %s have been completed', command)
                command.handle_result(self, report)

            pending = self._swap_futures()
            pending.extend(incomplete)
            log.debug('still incomplete tasks: %s', incomplete)

        log.debug('All tasks have been completed')

    def _produce_commands(self):
        targets = {'alpha', 'beta', 'gamma', 'delta', 'epsilon'}
        for entry in targets:
            self._exec(DummyCommand(entry))

    def _exec(self, command):
        f = self._executor.submit(command)
        self._futures.append(f)
        return f

    def _swap_futures(self):
        current = self._futures
        self._futures = []
        return current


class DataCommandBase(abc.ABC):
    def __call__(self):  # executed into separate thread
        return self

    def handle_result(self, monitor, report):
        raise NotImplementedError


class DummyCommand(DataCommandBase):
    def __init__(self, name):
        self.name = name
        self.result = None

    def __call__(self):
        log.info('Dummy executing mode')
        # do some processing here
        # (better do not change any objects outside self)
        self.result = random.randint(0, 1000)

        delay = random.randint(0, 5000) / 1000
        log.debug('sleep for %.03f seconds', delay)
        time.sleep(delay)

        if self.name == 'gamma':
            raise AttributeError('forced error to check error handling')

        return super().__call__()

    def handle_result(self, monitor, report):
        report.dummy_metric.labels(self.name).set(self.result)


if __name__ == '__main__':
    main()
