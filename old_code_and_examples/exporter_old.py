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

from common import getLogger
log = getLogger()


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

    # отключить встроенные метрики языка
    prometheus_client.REGISTRY.unregister(prometheus_client.PROCESS_COLLECTOR)
    prometheus_client.REGISTRY.unregister(prometheus_client.PLATFORM_COLLECTOR)
    prometheus_client.REGISTRY.unregister(prometheus_client.GC_COLLECTOR)

    concurrency = max(MIN_THREAD_COUNT, args.concurrency)
    # Создание пула потоков/нитей
    executor = futures.ThreadPoolExecutor(concurrency)

    # обертка над нитками которая запускает их и проверят статусы
    monitor = Monitor(executor)


    # Создание нового объекта Exporter (отнаследованного от prometheus_client.registry.Collector)
    exporter = Exporter()
    # зарегестрировать объект как экспортер для того что бы его метод
    # collect()  был вызван при обращении по https(s) к экспортеру.
    # При этом этот класс должен быть написан специальным образом -
    # отнаследован от prometheus_client.registry.Collector и иметь
    # метода collect()
    # Примерно вот так (см ниже):
    # class Exporter(prometheus_client.registry.Collector):
    prometheus_client.REGISTRY.register(exporter)

    # Запуск веб сервера в отдельном потоке (то что он в отдельном потоке - неявно, так устроен метод)
    prometheus_client.start_http_server(args.http_port)
    while True:
        # так как вся работа идет в отдельных потоках то в основном потоке только спать
        monitor.itteration(exporter)
        time.sleep(60)
        log.debug('[main loop] sleep iteration')


def collectData():


def init_logging(debug=False):
    root = getLogger()

    stderr_handler = logging.StreamHandler(stream=sys.stdout)
    stderr_handler.setFormatter(logging.Formatter(
        '## %(asctime)s %(threadName)s %(message)s'))
    root.addHandler(stderr_handler)

    if debug:
        root.setLevel(logging.DEBUG)
    else:
        root.setLevel(logging.INFO)


class Exporter(prometheus_client.registry.Collector):
    # Это объект для работы с метриками прометеуса
    # этот объект регестрируется в регистри и опрашивает все зарегестрированные объекты и выхзывает метод  collect
    def __init__(self):
        log.debug("[Exporter] __init__")
        self._report = Report()

    def set_report(self, report):
        self._report = report

    def collect(self) -> Iterable[Metric]:
        # Так как сбор данных происходит в отдельной нитке то в этом месте никакого
        # Data Scraping  не происходит, только отдаются уже готовые данные которые предварительно
        # сохранены другой ниткой в обекте report
        log.debug('[Exporter] collect: Start data scraping')
        # Класс report предназначен для хранения метрик
        report = Report()
        #try:
        #    #self._monitor.iteration(report)
        #except ValueError as e:
        #    log.error('[Exporter] collect: Scrapping error: {}', e)
        #    report.scraping_postpone.inc()
        #    # return [self.scrapping_postpone]
        #    return report.error_view()
        #finally:
        #    log.debug('[Exporter] collect: Done data scraping')
        return self._report


class _Counter:
    _overflow_at = 0xffffffff

    def __init__(self):
        log.debug('[Counter] __init__')
        self.value = 0

    def inc(self):
        log.debug('[Counter] inc: {}-->{}'.format(self.value, self.value+1))
        self.add(1)

    def add(self, value):
        if value < 0:
            raise ValueError(
                f'{type(self).__name__}.add() do not support negative values '
                f'(value=={value})')
        self.value += value

        log.debug('[Counter] add: {o}  {v}'.format(o=self._overflow_at, v=self.value))

        if self._overflow_at < self.value:
            value = value - self._overflow_at


class Report(collections.abc.Iterable):
    scraping_errors = _Counter()   # app globals
    scraping_postpone = _Counter() # app globals

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
    # Принимает на вход только екземпляр класса executor
    # те потоки выполнения
    _lock = threading.Lock()

    def __init__(self, executor):
        self._executor = executor
        self._futures = []

    def iteration(self, report):
        #
        if not self._lock.acquire(blocking=False):
            # do not allow to start parallels execution
            # it can work into multirequest mode, but better not dp it
            raise ValueError('[Monitor] iteration: Busy')
        try:
            self._iteration(report)
        finally:
            self._lock.release()

    def _iteration(self, report):
        log.info('[Monitor] _iteration: Start monitor iteration')

        # запускает задачи для ниток
        self._produce_commands()

        log.debug('[Monitor] _iteration: Wait for tasks in threads to complete')

        pending = self._swap_futures()
        while pending:
            log.debug('[Monitor] _iteration: pending tasks: %s', pending)
            done, incomplete = futures.wait(
                pending, return_when=futures.FIRST_COMPLETED)
            for f in done:
                try:
                    command = f.result()
                except Exception:
                    log.error('[Monitor] _iteration: Error during processing of %s', f, exc_info=True)
                    report.scraping_errors.inc()
                    continue
                log.debug('[Monitor] _iteration: Command %s have been completed', command)
                command.handle_result(self, report)

            pending = self._swap_futures()
            pending.extend(incomplete)
            log.debug('[Monitor] _iteration: still incomplete tasks: %s', incomplete)

        log.debug('[Monitor] _iteration: All tasks have been completed')

    def _produce_commands(self):
        log.debug('[Monitor] _produce_commands start')
        targets = {'alpha', 'beta', 'gamma', 'delta', 'epsilon'}
        for entry in targets:
            log.debug('[Monitor] _produce_commands: _exec(DummyCommand({entry}))'.format(entry=entry))
            self._exec(DummyCommand(entry))

    def _exec(self, command):
        log.debug('[Monitor] _exec: {c}'.format(c=command))
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
        log.debug('[DummyCommand] __init__: {n}'.format(n=name))
        self.name = name
        self.result = None

    def __call__(self):
        log.info('[DummyCommand] __call__: Dummy executing mode')
        # do some processing here
        # (better do not change any objects outside self)
        self.result = random.randint(0, 1000)

        delay = random.randint(0, 5000) / 1000
        log.debug('[DummyCommand] __call__: sleep for %.03f seconds', delay)
        time.sleep(delay)

        if self.name == 'gamma':
            raise AttributeError('forced error to check error handling')

        return super().__call__()

    def handle_result(self, monitor, report):
        # Это метод который обрабатывает результаты работы нитки
        # который сохраняет результаты работы в метрики
        report.dummy_metric.labels(self.name).set(self.result)
        #


if __name__ == '__main__':
    main()
