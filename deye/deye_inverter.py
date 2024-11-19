from ._base import *
from .logger import getLogger


class DeyeInverter(object):

    def __init__(self, stick_logger_ip, stick_logger_serial, port=8899, mb_slave_id=1, logger=None, log_level=None ):
        self.stick_logger_ip = stick_logger_ip
        self.stick_logger_serial = stick_logger_serial
        self.port = port
        self.mb_slave_id = mb_slave_id

        # максимальное число регистров которые отдает инвертер за одну операцию
        # чтения, возможно это значение потребуется увеличить или уменьшить для
        # других моделей инверторов
        self.max_number_of_registers_to_read_in_request = 125
        if logger:
            self.logger = logger
        else:
            self.logger = getLogger("DeyeInverter")

        # По-умолчанию все что связано с дебагом отключено
        self.verbose = False

        if log_level:
            logger.setLevel(log_level)
            if (log_level == "DEBUG") or ("DEBUG" in os.environ):
                self.verbose = True

        self.logger.debug("DeyeInverter __init__")

        self.sleep_on_inverter_read_error = 60
        self.max_read_attempts = 10
        self.inverter_read_raw_result_all_registers = []
        # https://github.com/kellerza/sunsynk/blob/main/src/sunsynk/definitions/single_phase.py
        #
        # id: номер регистра
        # units: единицы измерения, пустая для безразмерных величин вроде статуса OK/Fail или Connected/Disconnected
        # scale: множитель, если не указан принимается равным 1
        # offset: смещение, если не указан принимается равным 0
        # do_rounding: требуется ли делать округление, если не указан принимается False
        # decode_method: Если определен, то для значения требуется специальный метод декодирования
        # quantity: число регистров, в которых содержится искомая величина, по умолчанию это 1 регистр
        self.well_known_registers = {
            'battery_temperature':    {'id': 182, 'units': 'C',  'scale': 0.1,     'offset': -100 },
            'battery_voltage':        {'id': 183, 'units': 'V',  'scale': 0.01  },
            'battery_soc':            {'id': 184, 'units': '%' },
            'battery_charge_limit':   {'id': 314, 'units': 'A' },
            'battery_dischage_limit': {'id': 315, 'units': 'A' },
            'grid_frequency':         {'id': 79,  'units': 'Hz', 'scale': 0.01 },
            'grid_power':             {'id': 169, 'units': 'W',  'scale': -1   },
            'grid_ld_power':          {'id': 167, 'units': 'W',  'scale': -1   },
            'grid_l2_power':          {'id': 168, 'units': 'W',  'scale': -1   },
            'grid_voltage':           {'id': 150, 'units': 'V',  'scale': 0.1,     'do_rounding': True },
            'grid_current':           {'id': 160, 'units': 'A',  'scale': 0.01,    'do_rounding': True },
            'grid_ct_power':          {'id': 172, 'units': 'W',  'scale': -1    },
            'load_power':             {'id': 178, 'units': 'W'},
            'load_l1_power':          {'id': 176, 'units': 'W'},
            'load_l2_power':          {'id': 177, 'units': 'W'},
            'load_frequency':         {'id': 192, 'units': 'Hz', 'scale': 0.01 },
            'overall_state':          {'id': 59,  'units': ''  , 'decode_method': self.decode_overall_state },
            'fault_state':            {'id': 103, 'units': ''  , 'decode_method': self.decode_fault_state, 'quantity': 4 },
            'grid_connection':        {'id': 194, 'units': ''  , 'decode_method': self.decode_grid_connection }
        }

        self.grid_connection_status = {
            0: 'OFF',
            1: 'ON'
        }

        self.inverter_state = {
            0: "standby",
            1: "selfcheck",
            2: "ok",
            3: "alarm",
            4: "fault",
            5: "activating"
        }

        self.faults = {
            13: "Working mode change",
            18: "AC over current",
            20: "DC over current",
            23: "AC leak current or transient over current",
            24: "DC insulation impedance",
            26: "DC busbar imbalanced",
            29: "Parallel comms cable",
            35: "No AC grid",
            42: "AC line low voltage",
            47: "AC freq high/low",
            56: "DC busbar voltage low",
            63: "ARC fault",
            64: "Heat sink tempfailure",
        }

        # найти максимальное значение известного регистра что бы определить сколько операций чтения
        # (читая по max_number_of_registers_to_read_in_request за одну операцию) потребуется
        # что бы прочитать все регситры которые хочется прочитать. (или точнее что бы в прочитанных регистрах
        # точно оказались те, значения которых известно как декодировать)
        # Когечно тут можно было хахардкодить и черт с ним - но хочется в будущем минимально смотреть в кода,
        # изменяя только список регистров, если получится узнать что значит еще какой-то из них
        self.max_register_number = 0
        # Определить максимальное значение известного регистра
        for k, v in self.well_known_registers.items():
            if v['id'] > self.max_register_number:
                self.max_register_number =  v['id']
        # Целое число чтений (деление целочисленное) но почти всегда это значение будет на 1 меньше чем нужно
        # например число чтений за раз = 125 (прочитать регистры от 0 до 124 включительно) и предположим что
        # максимальный известный регистр имеет номер 130 -  тогда при делении 130 // 126 = 1
        self.inverter_registers_reads_number  = self.max_register_number // self.max_number_of_registers_to_read_in_request

        # Проверить что бы максимальный регистр точно попадал в диапазон читаемых, и если нет то расширить диапазон
        # -1  в выражении означает что последний прочитанный регистр имеет на 1 меньший адрес так как счет начитается с 0
        if (self.max_register_number > (self.max_number_of_registers_to_read_in_request * self.inverter_registers_reads_number - 1 ) ):
            self.inverter_registers_reads_number = self.inverter_registers_reads_number + 1

        self.logger.debug("max_register_number: {} Reads to be done: {} ({} registers on each read)".format(
            self.max_register_number, self.inverter_registers_reads_number, self.max_number_of_registers_to_read_in_request)
        )
        self.logger.debug("Will read the following registers. First register number: 0, last register number: {}".format(
            self.max_number_of_registers_to_read_in_request * self.inverter_registers_reads_number - 1
            )
        )


    def default_simple_decoder(self, data):
        # простой декодер который просто возвращает 1-й элемент
        # List-а - наиболее частая операция над результатом запроса
        return data[0]


    def decode_overall_state(self, overall_state):
        # Декодер для статуса - возвращает человекочитаемый статус
        #  в виде строки а не числа/кода состояния
        if len(overall_state) != 1:
            raise ValueError("Expected size of list is 1")
        else:
            return self.inverter_state[overall_state[0]]


    def decode_grid_connection(self, grid_connection):
        if len(grid_connection) != 1:
            raise ValueError("Expected size of list is 1")
        else:
            return self.grid_connection_status[grid_connection[0]]

    def decode_fault_state(self, fault_state):
        """Decode Inverter faults."""
        self.logger.debug("[decode_fault_state] Decoding fault state raw data: {}".format(fault_state))
        err = []
        off = 0
        register_number = 0
        for register_value in fault_state:
            for bit_number in range(16):
                mask = 1 << bit_number
                masked = mask & register_value

                if masked:
                    self.logger.debug("[decode_fault_state] bit number:{bit_number}".format(bit_number=bit_number))
                    self.logger.debug("[decode_fault_state] register  :{register:016b}".format(register=register_value))
                    self.logger.debug("[decode_fault_state] mask      :{mask:016b}".format(mask=mask))
                    self.logger.debug("[decode_fault_state] masked    :{masked:016b}".format(masked=masked))
                    self.logger.debug("Bit {bit_number} is SET in the regiater {register_number}, " )
                    msg = f"F{bit_number+off+1:02} " + self.faults.get(off + mask, "")
                    print("MSG={}".format(msg))
                    err.append(msg.strip())
                off = off +  16
            register_number = register_number + 1
        if err:
            return ", ".join(err)
        else:
            return "No Errors Detected"


    def read_registers(self):
        self.logger.debug("Starting data collecting from inverter: {}:{}".format(
            self.stick_logger_ip,  self.port)
        )


        all_raw_registers = []
        # Насколько я могу судить quantity это число определяющее сколько данных читать
        # Например можно прочитать 2 регистра по-одному
        # inverter_read_raw_result = modbus.read_holding_registers(register_addr=314, quantity=1)
        # self.logger.debug(inverter_read_raw_result)
        # inverter_read_raw_result = modbus.read_holding_registers(register_addr=315, quantity=1)
        # self.logger.debug(inverter_read_raw_result)
        #
        # В результате будет
        # [10]  (reg 314)
        # [200] (reg 315)
        # или прочитать 2 сразу
        # inverter_read_raw_result = modbus.read_holding_registers(register_addr=314, quantity=2)
        # self.logger.debug(inverter_read_raw_result)
        # получив [10, 200]
        #
        # Максимальное число регистров читаемых за 1 раз - 125

        self.inverter_read_raw_result_all_registers = []
        read_start_register = 0
        read_attempts = self.max_read_attempts
        while read_attempts:
            try:
                modbus = pysolarmanv5.PySolarmanV5(
                    self.stick_logger_ip, self.stick_logger_serial,
                    port=self.port, mb_slave_id=self.mb_slave_id,
                    verbose=self.verbose, logger=self.logger
                    )
                for read_number in range(1, self.inverter_registers_reads_number+1):
                    self.logger.debug("Read number: {}. Rading registers from {} to {}".format(
                            read_number,
                            read_start_register,
                            read_start_register + self.max_number_of_registers_to_read_in_request -  1
                        )
                    )
                    inverter_read_raw_result = modbus.read_holding_registers(
                        register_addr=read_start_register,
                        quantity=self.max_number_of_registers_to_read_in_request
                    )
                    self.logger.debug("Read result (raw): {}".format(inverter_read_raw_result))
                    self.inverter_read_raw_result_all_registers = self.inverter_read_raw_result_all_registers + inverter_read_raw_result
                    read_start_register = read_start_register + self.max_number_of_registers_to_read_in_request
                break
            except pysolarmanv5.pysolarmanv5.NoSocketAvailableError:
                self.logger.error("pysolarmanv5.pysolarmanv5.NoSocketAvailableError: Sleeping for {sleep_on_inverter_read_error} seconds".format(
                        sleep_on_inverter_read_error=self.sleep_on_inverter_read_error
                    )
                )
                # just sleep and retry
                time.sleep(self.sleep_on_inverter_read_error)
                modbus = None
                read_attempts = read_attempts -1
            except Exception as E:
                raise(E)

        self.logger.debug("All registers are: {}".format(self.inverter_read_raw_result_all_registers))

    def decode_registers(self):
        output = {}
        for register_name, register_details in self.well_known_registers.items():
            register_id = register_details['id']

            try:
                quantity = register_details.get('quantity', 1)
                register_value = self.inverter_read_raw_result_all_registers[register_id:register_id+quantity]
                self.logger.debug("Decoding register id {}, register name: {}, register value (raw): {}".format(
                        register_id,
                        register_name,
                        register_value
                    )
                )
                # Если определен метод декодирования то использовать его для декодирования результата,
                # если нет то использовать метод by default.
                decode_method = register_details.get('decode_method', self.default_simple_decoder)
                decoded_result = decode_method(register_value)


                # В зависимости от типа данных нужно или посчитать смещение/масштаб/округление
                # или не делать ничего если результат - безразмерный, это значит что это текстовая строка
                if ( register_details['units'] in ['C', 'V', '%', 'A', 'Hz', 'W'] ):
                    # Если есть множитель - домножить на него, если не определен то домножить на 1
                    scale = register_details.get('scale', 1)
                    # Если точка отсчета не 0 - то сместить на соответвующее число
                    offset = register_details.get('offset', 0)
                    # получить человекочитаемый результат масштабированием и смещением
                    human_readable_result = decoded_result * scale + offset

                    # Округлить, если требуется
                    do_rounding = register_details.get('do_rounding', False)
                    if do_rounding:
                        human_readable_result = round(human_readable_result)
                else:
                    human_readable_result = decoded_result

                self.logger.info("Decoding register id {}, register name: {}, register value (decoded): {} {}".format(
                        register_id,
                        register_name,
                        human_readable_result,
                        register_details['units']
                    )
                )
                output[register_name] = {}
                output[register_name]['value'] = human_readable_result
                output[register_name]['units'] = register_details['units']
            except Exception as E:
                self.logger.error("Exception {} for register {}, skipping register".format(E, register_name))
                pass



        message = json.dumps(output, indent=4)
        self.logger.info(message)
        return output

#            except pysolarmanv5.pysolarmanv5.NoSocketAvailableError:
#                self.logger.error("pysolarmanv5.pysolarmanv5.NoSocketAvailableError: Sleeping for {sleep_on_inverter_read_error} seconds".format(sleep_on_inverter_read_error=sleep_on_inverter_read_error))
#                # just sleep and retry
#                time.sleep(sleep_on_inverter_read_error)
#                modbus = None
#                return
#            except Exception as E:
#                raise(E)












