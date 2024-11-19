#!/usr/bin/env python3

import json
import logging
import os
import prometheus_client
import prometheus_client.core
import select
import sys
import time
import threading
import queue
import paho.mqtt.client as mqtt
# custom module
import deye

log = logging.getLogger()

def main():

    empty_queue_sleep_seconds = 10
    mqtt_send_sleep_seconds = 30
    data_collection_period_seconds = 20
    sleep_on_data_collection_error_seconds = 60
    # Если в течении этого числа секунд нет новых данных
    # то считаем что данные устарели
    data_is_outdated_after_collected_seconds = 600

    init_logging(True)
    log.info('[main] Starting ...')

    if ("DEYE_LOGGER_IP" in os.environ) and ("DEYE_LOGGER_SERIAL" in os.environ):
        log.info("[main] Found inverter configuration env variables: DEYE_LOGGER_IP, DEYE_LOGGER_SERIAL")
    else:
        raise ValueError("Please define DEYE_LOGGER_IP and  DEYE_LOGGER_SERIAL environment variables")

    # Очередь для передачи данных в поток експортера
    collected_data_queue_for_exporter = queue.LifoQueue(maxsize=1)
    # Определяем переменную для очереди, но создаем ее только если это нужно
    # (нужно когда определены параметры MQTT)
    collected_data_queue_for_mqtt = None

    # Если параметры MQTT определены то создавать отдельный поток для отправки в в MQTT
    # и отдельную очередь для таких сообщений
    if ( ('MQTT_HOST' in os.environ) and ('MQTT_USERNAME' in os.environ) and ('MQTT_PASSWORD' in os.environ) ):
        mqtt_host = os.environ.get('MQTT_HOST')
        mqtt_username = os.environ.get('MQTT_USERNAME')
        mqtt_password = os.environ.get('MQTT_PASSWORD')
        mqtt_topic = os.environ.get('MQTT_TOPIC', 'homeassistant/sensor/inverter/state')

        collected_data_queue_for_mqtt = queue.LifoQueue(maxsize=1)

        th_send_data_to_mqtt = threading.Thread(target=send_data_to_mqtt, args=(
                collected_data_queue_for_mqtt,
                empty_queue_sleep_seconds,
                mqtt_send_sleep_seconds,
                mqtt_topic,
                mqtt_host,
                mqtt_username,
                mqtt_password,
            ),
            name='send_data_therad')

        th_send_data_to_mqtt.daemon = True
        th_send_data_to_mqtt.start()
        log.info('[main] MQTT thread have been started')
    else:
        log.debug("[main] MQTT variables are not defined, thread is not started")

    # Создаем отдельный поток для сбора данных который будет опрашивать инвертор
    th_collect_data = threading.Thread(target=collect_data, args=(
            collected_data_queue_for_mqtt,
            collected_data_queue_for_exporter,
            data_collection_period_seconds,
            sleep_on_data_collection_error_seconds
        ),
        name='collect_data_therad')
    th_collect_data.daemon = True
    th_collect_data.start()


    idx = 0

    # отключить встроенные метрики языка
    prometheus_client.REGISTRY.unregister(prometheus_client.PROCESS_COLLECTOR)
    prometheus_client.REGISTRY.unregister(prometheus_client.PLATFORM_COLLECTOR)
    prometheus_client.REGISTRY.unregister(prometheus_client.GC_COLLECTOR)
    # зарегестрировать объект как экспортер для того что бы его метод
    # collect()  был вызван при обращении по https(s) к экспортеру.
    # При этом этот класс должен быть написан специальным образом -
    # отнаследован от prometheus_client.registry.Collector и иметь
    # метода collect()
    # Примерно вот так (см ниже):
    # class Exporter(prometheus_client.registry.Collector):
    prometheus_client.REGISTRY.register(CustomCollector(collected_data_queue_for_exporter, data_is_outdated_after_collected_seconds))


    prometheus_exporter_port = int(os.environ.get('HTTP_PORT', 8181))
    prometheus_exporter_host = os.environ.get('HTTP_HOST', '127.0.0.1')
    log.info("[main] Staring web server on {}:{}".format(prometheus_exporter_host, prometheus_exporter_port))
    # Запуск веб сервера в отдельном потоке (то что он в отдельном потоке - неявно, так устроен метод)
    prometheus_client.start_http_server(prometheus_exporter_port, addr=prometheus_exporter_host)

    while True:
        log.info('[main] Main iteration (%s)', idx)
        th_collect_data.join(timeout=3)
        if not th_collect_data.is_alive():
            break

        idx = idx + 1
        #if 2 < idx:
        #    log.info('close pipe (write end)')

    log.info('[main] Main end, exiting')


def collect_data(collected_data_queue_for_mqtt, collected_data_queue_for_exporter, data_collection_period_seconds, sleep_on_data_collection_error_seconds):
    log.info('[collect_data] Entering thread collect_data')
    collected_data = {}
    while True:
        log.info('[Thread info: collect_data]')

        stick_logger_ip = os.environ.get("DEYE_LOGGER_IP")
        stick_logger_serial = int(os.environ.get("DEYE_LOGGER_SERIAL"))
        deye_inverter = deye.DeyeInverter(stick_logger_ip, stick_logger_serial)

        try:
            deye_inverter.read_registers()
            collected_data = deye_inverter.decode_registers()
        except Exception as E:
            log.error("[collect_data] Error collecting data {}, sleepping for {} seconds  ".format(
                E, sleep_on_data_collection_error_seconds)
            )
            time.sleep(sleep_on_data_collection_error_seconds)
            # так как данных нет то перейти к следующей иттерации и попробовать прочитать снова
            continue

        collected_data['data_collected_at'] = time.time()
        log.debug("[collect_data] Collected Data: {}".format(collected_data))

        for collected_data_queue in [collected_data_queue_for_exporter, collected_data_queue_for_mqtt]:
            log.debug("[collect_data] Sending data to queue {}".format(collected_data_queue))
            # Пробуем отправить данные если очередь определена
            # (не определена может быть очередь для MQTT если соответвующие переменные не передали)
            if collected_data_queue:
                try:
                    collected_data_queue.put(collected_data, block=False)
                except queue.Full:
                    log.debug("[collect_data] Queue is full, removing last data")
                    old_collected_data = collected_data_queue.get(block=False)
                    log.debug("[collect_data] Removing old dtata: {}, replacing with new data {}".format(
                            old_collected_data,
                            collected_data
                        )
                    )

        log.debug("[collect_data] Sleeping for {} before next collection period".format(data_collection_period_seconds))
        time.sleep(data_collection_period_seconds)

    log.error('[collect_data] Finishing thread (this is not expected, it should be an endless loop!!!')

def on_publish(client, userdata, mid, reason_code, properties):
    # reason_code and properties will only be present in MQTTv5. It's always unset in MQTTv3
    try:
        log.debug("[on_publish] client: {}, userdata: {}, mid: {}, reason_code: {}, properties: {}".format(
                client, userdata, mid, reason_code, properties)
        )
        userdata.remove(mid)
    except KeyError:
        print("on_publish() is called with a mid not present in unacked_publish")
        print("This is due to an unavoidable race-condition:")
        print("* publish() return the mid of the message sent.")
        print("* mid from publish() is added to unacked_publish by the main thread")
        print("* on_publish() is called by the loop_start thread")
        print("While unlikely (because on_publish() will be called after a network round-trip),")
        print(" this is a race-condition that COULD happen")
        print("")
        print("The best solution to avoid race-condition is using the msg_info from publish()")
        print("We could also try using a list of acknowledged mid rather than removing from pending list,")
        print("but remember that mid could be re-used !")


def send_data_to_mqtt(collected_data_queue, empty_queue_sleep_seconds, mqtt_send_sleep_seconds, topic, mqtt_host, mqtt_username, mqtt_password):
    log.info('[send_data_to_mqtt] Entering thread send_data_to_mqtt')
    commected_data = {}
    while True:
        try:
            collected_data = collected_data_queue.get(block=False)
            # Удлаить отметку времени - она нужна только для для части экспортера, и не
            # нужна для MQTT
            if 'data_collected_at' in collected_data:
                collected_data.pop('data_collected_at')
            mqtt_message = {}
            for k, v in collected_data.items():
                mqtt_message[k] = v['value']
            log.debug("[send_data_to_mqtt] Got data from queue: {}, mqtt_message: {}".format(
                collected_data, mqtt_message)
            )

            mqtt_message = json.dumps(mqtt_message)

            unacked_publish = set()
            mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            mqttc.password = mqtt_password
            mqttc.username = mqtt_username
            mqttc.host = mqtt_host
            mqttc.on_publish = on_publish

            mqttc.user_data_set(unacked_publish)
            mqttc.connect(mqtt_host)
            log.debug("[send_data_to_mqtt] Connect done")
            mqttc.loop_start()

            # Our application produce some messages
            msg_info = mqttc.publish(topic, mqtt_message, qos=1)
            unacked_publish.add(msg_info.mid)
            # Wait for all message to be published
            while len(unacked_publish):
                time.sleep(0.1)
                # Due to race-condition described above, the following way to wait for all publish is safer
            msg_info.wait_for_publish()
            mqttc.disconnect()
            log.debug("[send_data_to_mqtt] Disconnect")
            mqttc.loop_stop()


            log.debug("[send_data_to_mqtt] Sleeping for {} second(s) before next MQTT queue update".format(mqtt_send_sleep_seconds))
            time.sleep(mqtt_send_sleep_seconds)
        except queue.Empty:
            log.debug("[send_data_to_mqtt] Queue is Empty (First data collection or data was not updated), sleeping for {} second(s)".format(empty_queue_sleep_seconds))
            time.sleep(empty_queue_sleep_seconds)
    log.error('[send_data_to_mqtt] Finishing thread (this is not expected, it should be an endless loop!!!')




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




class CustomCollector(object):

    def __init__(self, exporter_queue, data_is_outdated_after_collected_seconds):
        self.exporter_queue = exporter_queue
        self.collected_data = {}
        self.data_collected_at = time.time()
        self.data_is_outdated_after_collected_seconds = data_is_outdated_after_collected_seconds

    def describe(self):
        return [self._make_gauge_metric_family()]

    def _make_gauge_metric_family(self):
        return prometheus_client.core.GaugeMetricFamily(
                'deye_inverter_metrics',
                'Metrics from Deye inverter',
                labels=['metic_name', 'metric_unit']
            )

    def _make_info_metric_family(self):
        return prometheus_client.core.InfoMetricFamily(
                'deye_inverter_metrics_info',
                'Metrics from Deye inverter (info)',
                labels=['metic_name', 'metric_string_value']
            )

    def collect(self):
        gauge_metrics = self._make_gauge_metric_family()
        info_metrics = self._make_info_metric_family()
        gauge_units_list = ['C', 'V', '%', 'A', 'Hz', 'W']
        try:
            # Получить данные из очереди, если они там есть
            self.collected_data = self.exporter_queue.get(block=False)
            log.debug("[collect] Got data from queue: {}".format(self.collected_data))
        except queue.Empty:
            # Если данных ы очереди нет то работаем с данными полученными на прошлой итерации,
            # если они не устарели
            log.debug("[collect] Queue is Empty, Data is not colleced yet")

        # Проверить есть ли ключ 'data_collected_at' в данных,
        # его может не быть если например он был удален на предыдущем опросе, и за время
        # между опросами новых данных не появилось
        if 'data_collected_at' in self.collected_data:
            log.debug("[collect] data_collected_at = {}".format(self.data_collected_at))
            self.data_collected_at = self.collected_data.pop('data_collected_at')

        now_time = time.time()
        data_commected_ago_seconds = now_time - self.data_collected_at
        if ( data_commected_ago_seconds > self.data_is_outdated_after_collected_seconds):
            log.error("[collect] Data from queue is outdated: Collected {} seconds ago, data is outdated after {}".format(
                data_commected_ago_seconds,
                self.data_is_outdated_after_collected_seconds
                )
            )
            # данные устарели - с ними нельзя работать, просто отбрасываем
            self.collected_data = {}

        for metric_name, metric_data in self.collected_data.items():
            log.debug("[collect] Metric: {}, Value: {} Units {}".format(metric_name, metric_data['value'], metric_data['units']))

            if ( metric_data['units'] in gauge_units_list ):
                log.debug("[collect] {} is gauge ({}) Value: {}".format(metric_name, metric_data['units'], metric_data['value']))
                gauge_metrics.add_metric([metric_name, metric_data['units']], metric_data['value'])
            elif metric_data['units'] == '':
                log.debug("[collect] {} is INFO ({}) Value: {}".format(metric_name, metric_data['units'], metric_data['value']))
                info_metrics.add_metric([metric_name], {metric_name: str(metric_data['value'])})
            # Если юнит не один из известных и не пустой то что делать с таким не ясно - пропускаем
            else:
                log.error("Nothing to do with metric: {}, value: {}".format(
                    metric_name, str(metric_data))
                )
        log.debug("[collect] gauge_metrics: {}".format(gauge_metrics))
        log.debug("[collect] info_metrics: {}".format(info_metrics))

        return [gauge_metrics, info_metrics]


if __name__ == '__main__':
    main()

