"""
Pure binary FIT writer — no fit-tool dependency.
Writes a complete activity FIT file with optional embedded developer fields.
VERSION: beta-2026-04-08 (fix session field numbers per FIT SDK profile 21.200:
  24=total_training_effect, 137=anaerobic_TE, 168=training_load_peak,
  169/170/180=enhanced respiration rates; fixes MTB Dynamics ghost tab)
"""
import struct

FIT_EPOCH = 631065600  # 1989-12-31 00:00:00 UTC in Unix seconds

_CRC_TABLE = [
    0x0000,0xCC01,0xD801,0x1400,0xF001,0x3C00,0x2800,0xE401,
    0xA001,0x6C00,0x7800,0xB401,0x5000,0x9C01,0x8801,0x4400,
]

def _crc(data):
    crc = 0
    for b in data:
        tmp = _CRC_TABLE[crc & 0xF]; crc = (crc >> 4) & 0x0FFF; crc ^= tmp ^ _CRC_TABLE[b & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]; crc = (crc >> 4) & 0x0FFF; crc ^= tmp ^ _CRC_TABLE[(b >> 4) & 0xF]
    return crc

def _u8(v):  return struct.pack('<B', v & 0xFF)
def _u16(v): return struct.pack('<H', v & 0xFFFF)
def _u32(v): return struct.pack('<I', v & 0xFFFFFFFF)
def _s32(v): return struct.pack('<i', int(v))
def _f32(v): return struct.pack('<f', float(v))

INVALID_U8  = 0xFF
INVALID_U16 = 0xFFFF
INVALID_U32 = 0xFFFFFFFF
INVALID_S32 = 0x7FFFFFFF
INVALID_F32 = float('nan')


def _enc_pp4(v):
    """Encode a power_phase value as 4 bytes (seated_start, seated_end, standing_start, standing_end).
    Input: list/tuple of 2 or 4 values in DEGREES (as returned by the Garmin SDK).
    The FIT file stores raw uint8 angular units: raw = round(degrees × 256 / 360).
    """
    if v is None:
        return b'\xff\xff\xff\xff'
    if isinstance(v, (list, tuple)):
        vals = list(v)
    else:
        try:
            s = str(v).replace('[','').replace(']','').replace('(','').replace(')','')
            vals = [x.strip() for x in s.split(',') if x.strip()]
        except Exception:
            return b'\xff\xff\xff\xff'
    # Convert degrees → raw uint8 angular units, pad to 4 with 0xFF
    raw = []
    for x in vals:
        try:
            deg = float(x)
            raw.append(max(0, min(254, int(round(deg * 256.0 / 360.0)))))
        except Exception:
            raw.append(0xFF)
    while len(raw) < 4:
        raw.append(0xFF)
    return bytes(raw[:4])


def _enc_u16_pair(v):
    """Encode a pair of uint16 values (e.g. avg_power_position: [seated, standing])."""
    if v is None:
        return b'\xff\xff\xff\xff'
    if isinstance(v, (list, tuple)):
        vals = list(v)
    else:
        try:
            s = str(v).replace('[','').replace(']','').replace('(','').replace(')','')
            vals = [x.strip() for x in s.split(',') if x.strip()]
        except Exception:
            return b'\xff\xff\xff\xff'
    result = b''
    for i in range(2):
        if i < len(vals) and vals[i] not in (None, '', 'None'):
            try:
                result += _u16(max(0, min(65534, int(round(float(vals[i]))))))
            except Exception:
                result += b'\xff\xff'
        else:
            result += b'\xff\xff'
    return result


def _enc_u8_pair(v):
    """Encode a pair of uint8 values (e.g. avg_cadence_position: [seated, standing])."""
    if v is None:
        return b'\xff\xff'
    if isinstance(v, (list, tuple)):
        vals = list(v)
    else:
        try:
            s = str(v).replace('[','').replace(']','').replace('(','').replace(')','')
            vals = [x.strip() for x in s.split(',') if x.strip()]
        except Exception:
            return b'\xff\xff'
    result = b''
    for i in range(2):
        if i < len(vals) and vals[i] not in (None, '', 'None'):
            try:
                result += _u8(max(0, min(254, int(round(float(vals[i]))))))
            except Exception:
                result += b'\xff'
        else:
            result += b'\xff'
    return result

def _def_msg(ln, gmn, fields, dev_fields=None):
    """
    Build a FIT definition message.
    fields: list of (field_num, size, base_type)
    dev_fields: list of (field_num, size, dev_data_index)
    """
    is_dev = bool(dev_fields)
    rh = 0x40 | (0x20 if is_dev else 0x00) | (ln & 0x0F)
    buf = bytes([rh, 0x00, 0x00]) + _u16(gmn) + bytes([len(fields)])
    for fn, sz, bt in fields:
        buf += bytes([fn, sz, bt])
    if is_dev:
        buf += bytes([len(dev_fields)])
        for fn, sz, di in dev_fields:
            buf += bytes([fn, sz, di])
    return buf

def _data_hdr(ln): return bytes([ln & 0x0F])

def _ts(dt_or_fit):
    """Convert datetime or FIT-seconds int to FIT timestamp (seconds since 1989-12-31)."""
    import datetime as _dt
    if isinstance(dt_or_fit, (int, float)):
        v = int(dt_or_fit)
        if v > 1_000_000_000: return v - FIT_EPOCH  # Unix seconds
        return v  # already FIT seconds
    if dt_or_fit.tzinfo is None:
        dt_or_fit = dt_or_fit.replace(tzinfo=_dt.timezone.utc)
    else:
        dt_or_fit = dt_or_fit.astimezone(_dt.timezone.utc)
    _FE = _dt.datetime(1989,12,31,0,0,0,tzinfo=_dt.timezone.utc)
    return int((dt_or_fit - _FE).total_seconds())


class FitWriter:
    # Each message type gets a UNIQUE local number — no redefinition
    # Some parsers (including intervals.icu) don't support local number reuse
    LN_FILE_ID    = 0
    LN_DEVICE_INF = 1
    LN_EVENT      = 2
    LN_DEV_ID     = 3   # DeveloperDataId
    LN_FIELD_DESC = 4   # FieldDescription
    LN_RECORD     = 5   # records WITHOUT dev fields
    LN_RECORD_DEV = 6   # records WITH dev fields
    LN_HRV        = 7
    LN_LAP        = 8
    LN_SESSION    = 9
    LN_ACTIVITY   = 10
    LN_USER_PROF  = 11
    LN_ZONES      = 12
    LN_MAIN = 0  # alias kept for compatibility — only used by write_file_id

    def __init__(self):
        self._body = bytearray()
        self._defined = set()

    def _write(self, data):
        self._body += data

    # ── Definition helpers ──────────────────────────────────────────────────

    def _ensure_def(self, key, def_bytes):
        if key not in self._defined:
            self._write(def_bytes)
            self._defined.add(key)

    # ── Individual message writers ──────────────────────────────────────────

    def write_file_id(self, ts_fit, manufacturer=1, product=0,
                      serial_number=None, number=None, product_name=''):
        product_name_bytes = (product_name or '').encode('utf-8')[:31] + b'\x00'
        product_name_bytes = product_name_bytes.ljust(32, b'\x00')
        self._ensure_def('file_id', _def_msg(self.LN_FILE_ID, 0, [
            (0,1,0x00),(1,2,0x84),(2,2,0x84),(3,4,0x8C),(4,4,0x86),(5,2,0x84),(8,32,0x07),
        ]))
        self._write(_data_hdr(self.LN_FILE_ID)
            + bytes([4])
            + _u16(manufacturer)
            + _u16(product)
            + _u32(serial_number if serial_number not in (None, 0) else INVALID_U32)
            + _u32(ts_fit)
            + _u16(number if number not in (None, 0xFFFF) else 0)
            + product_name_bytes)

    def write_device_info(self, ts_fit, device_index=0, manufacturer=1, product=0,
                          serial_number=None, software_version=None,
                          descriptor='Primary Device', product_name='', source_type=5):
        descriptor_bytes = (descriptor or '').encode('utf-8')[:63] + b'\x00'
        descriptor_bytes = descriptor_bytes.ljust(64, b'\x00')
        product_name_bytes = (product_name or '').encode('utf-8')[:31] + b'\x00'
        product_name_bytes = product_name_bytes.ljust(32, b'\x00')
        sw_raw = INVALID_U16 if software_version in (None, 0xFFFF) else int(round(float(software_version) * 100.0))
        self._ensure_def('device_info', _def_msg(self.LN_DEVICE_INF, 23, [
            (253,4,0x86),(0,1,0x02),(1,1,0x02),(2,2,0x84),(3,4,0x8C),(4,2,0x84),
            (5,2,0x84),(19,64,0x07),(25,1,0x00),(27,32,0x07),
        ]))
        self._write(_data_hdr(self.LN_DEVICE_INF)
            + _u32(ts_fit)
            + _u8(device_index)
            + bytes([4])
            + _u16(manufacturer)
            + _u32(serial_number if serial_number not in (None, 0) else INVALID_U32)
            + _u16(product)
            + _u16(sw_raw)
            + descriptor_bytes
            + _u8(source_type if source_type is not None else 5)
            + product_name_bytes)

    def write_event(self, ts_fit, event=0, event_type=0):
        self._ensure_def('event', _def_msg(self.LN_EVENT, 21, [
            (253,4,0x86),(0,1,0x00),(1,1,0x00),
        ]))
        self._write(_data_hdr(self.LN_EVENT) + _u32(ts_fit) + _u8(event) + _u8(event_type))

    def write_user_profile(self, weight_kg=None, height_m=None, gender=None,
                           age=None, resting_hr=None, max_hr=None,
                           activity_class=None, friendly_name=''):
        """Write a UserProfile message (gmn=3)."""
        # Fields: 0=friendly_name(string16), 1=gender(enum), 2=age(uint8),
        # 3=height(uint8,cm), 4=weight(uint16,×10→kg),
        # 8=resting_heart_rate(uint8), 11=default_max_biking_heart_rate(uint8),
        # 12=default_max_heart_rate(uint8), 17=activity_class(enum)
        name_bytes = (friendly_name or '').encode('utf-8')[:15] + b'\x00'
        name_bytes = name_bytes.ljust(16, b'\x00')
        self._ensure_def('user_profile', _def_msg(self.LN_USER_PROF, 3, [
            (0,16,0x07),(4,2,0x84),
            (1,1,0x00),(2,1,0x02),(3,1,0x02),
            (8,1,0x02),(11,1,0x02),(12,1,0x02),(17,1,0x00),
        ]))
        gender_raw = INVALID_U8 if gender is None else int(gender)
        age_raw = INVALID_U8 if age is None else max(0, min(254, int(age)))
        height_raw = INVALID_U8 if height_m is None else max(0, min(254, int(round(float(height_m) * 100))))
        weight_raw = INVALID_U16 if weight_kg is None else max(0, min(65534, int(round(float(weight_kg) * 10))))
        rhr_raw = INVALID_U8 if resting_hr is None else max(0, min(254, int(resting_hr)))
        mhr_raw = INVALID_U8 if max_hr is None else max(0, min(254, int(max_hr)))
        ac_raw = INVALID_U8 if activity_class is None else max(0, min(254, int(activity_class)))
        self._write(_data_hdr(self.LN_USER_PROF)
            + name_bytes + _u16(weight_raw)
            + _u8(gender_raw) + _u8(age_raw) + _u8(height_raw)
            + _u8(rhr_raw) + _u8(mhr_raw) + _u8(mhr_raw) + _u8(ac_raw))

    def write_zones_target(self, ftp=None, max_hr=None, threshold_hr=None):
        """Write a ZonesTarget message (gmn=7)."""
        # 254=message_index(uint16), 3=functional_threshold_power(uint16),
        # 1=max_heart_rate(uint8), 2=threshold_heart_rate(uint8),
        # 5=hr_calc_type(enum), 7=pwr_calc_type(enum)
        self._ensure_def('zones_target', _def_msg(self.LN_ZONES, 7, [
            (254,2,0x84),(3,2,0x84),
            (1,1,0x02),(2,1,0x02),(5,1,0x00),(7,1,0x00),
        ]))
        self._write(_data_hdr(self.LN_ZONES)
            + _u16(0)  # 254 message_index
            + _u16(ftp if ftp else INVALID_U16)        # 3 functional_threshold_power
            + _u8(max_hr if max_hr else INVALID_U8)    # 1 max_heart_rate
            + _u8(threshold_hr if threshold_hr else INVALID_U8)  # 2 threshold_heart_rate
            + _u8(1)   # 5 hr_calc_type = percent_lthr
            + _u8(1)   # 7 pwr_calc_type = percent_ftp
        )

    def write_developer_data_id(self, dev_idx, app_id_hex, application_version=None):
        """Write a DeveloperDataId message (gmn=207) using a Garmin-like layout."""
        self._ensure_def('dev_data_id', _def_msg(self.LN_DEV_ID, 207, [
            (0,16,0x0d),(1,16,0x0d),(4,4,0x86),(2,2,0x84),(3,1,0x02),
        ]))
        try:
            app_id = bytes.fromhex(app_id_hex.ljust(32,'0')[:32])
        except Exception:
            app_id = b'\xff' * 16
        app_ver = INVALID_U32 if application_version in (None, 0xFFFFFFFF) else int(application_version)
        buf = (_data_hdr(self.LN_DEV_ID)
            + b'\xff'*16 + app_id[:16] + _u32(app_ver) + _u16(INVALID_U16) + _u8(dev_idx))
        self._write(buf)

    def write_field_description(self, dev_idx, field_def_num, base_type,
                                 field_name, units='', size=4,
                                 native_field_num=None, native_mesg_num=None,
                                 scale=None, offset=None):
        """Write a FieldDescription message (gmn=206) using a Garmin-like layout."""
        name_bytes = (field_name or '').encode('utf-8')[:63] + b'\x00'
        name_bytes = name_bytes.ljust(64, b'\x00')
        units_bytes = (units or '').encode('utf-8')[:15] + b'\x00'
        units_bytes = units_bytes.ljust(16, b'\x00')
        size_raw = INVALID_U8 if size in (None, 0xFF) else int(size)
        scale_raw = 0x7F if scale is None else max(-127, min(127, int(scale)))
        developer_id_raw = _u16(INVALID_U16)
        # Garmin files often encode record message linkage as native_mesg=0xFF and native_field=20.
        native_field_raw = _u16(INVALID_U16 if native_field_num in (None, 0xFFFF) else int(native_field_num))
        native_mesg_raw = _u8(INVALID_U8 if native_mesg_num in (None, 0xFF) else int(native_mesg_num))
        self._ensure_def('field_desc', _def_msg(self.LN_FIELD_DESC, 206, [
            (3,64,0x07),(8,16,0x07),(13,2,0x84),(14,2,0x84),
            (0,1,0x02),(1,1,0x02),(2,1,0x02),(6,1,0x02),(7,1,0x01),(15,1,0x02),
        ]))
        buf = (_data_hdr(self.LN_FIELD_DESC)
            + name_bytes + units_bytes + developer_id_raw + native_field_raw
            + _u8(dev_idx) + _u8(field_def_num) + _u8(base_type)
            + _u8(size_raw) + struct.pack('<b', scale_raw) + native_mesg_raw)
        self._write(buf)

    def define_record(self, std_fields, dev_field_entries=None):
        """
        Define the record message. Must be called before write_record.
        std_fields: list of (field_num, size, base_type)
        dev_field_entries: list of (field_num, size, dev_data_index) or None
        """
        ln = self.LN_RECORD_DEV if dev_field_entries else self.LN_RECORD
        key = 'record_dev' if dev_field_entries else 'record'
        self._ensure_def(key, _def_msg(ln, 20, std_fields, dev_field_entries))
        self._rec_ln = ln
        self._rec_std = std_fields
        self._rec_dev = dev_field_entries or []

    def write_record(self, ts_fit, std_values, dev_values=None):
        """
        Write one record data message.
        std_values: list of bytes objects, one per std_field (in order)
        dev_values: list of bytes objects, one per dev_field (in order)
        """
        buf = _data_hdr(self._rec_ln) + _u32(ts_fit)
        for raw in std_values:
            buf += raw
        if dev_values:
            for raw in dev_values:
                buf += raw
        self._write(buf)

    def write_hrv(self, rr_ms_list):
        """Write one HRV message. rr_ms_list: list of up to 5 uint16 values (65535=invalid)."""
        self._ensure_def("hrv", _def_msg(self.LN_HRV, 78, [(0,10,0x04)]))
        padded = list(rr_ms_list) + [65535] * (5 - len(rr_ms_list))
        buf = _data_hdr(self.LN_HRV)
        for v in padded[:5]:
            buf += _u16(v if v is not None else 65535)
        self._write(buf)

    def write_lap(self, ts_fit, start_ts, elapsed, distance,
                  sport=2, sub_sport=0, lap_trigger=0,
                  avg_hr=None, max_hr=None,
                  avg_cad=None, max_cad=None, avg_pow=None, max_pow=None,
                  asc=None, desc=None,
                  total_calories=None, normalized_power=None,
                  left_right_balance=None,
                  time_standing=None, stand_count=None,
                  avg_left_torque_eff=None, avg_right_torque_eff=None,
                  avg_left_smoothness=None, avg_right_smoothness=None,
                  avg_combined_smoothness=None,
                  avg_left_pco=None, avg_right_pco=None,
                  avg_left_pp=None, avg_left_pp_peak=None,
                  avg_right_pp=None, avg_right_pp_peak=None,
                  avg_power_position=None, max_power_position=None,
                  avg_cadence_position=None, max_cadence_position=None,
                  message_index=0):
        # FIT SDK profile for Lap (mesg_num=19):
        # 253=timestamp, 254=message_index, 0=event, 1=event_type,
        # 2=start_time, 7=total_elapsed_time, 8=total_timer_time,
        # 9=total_distance, 11=total_calories,
        # 15=avg_heart_rate, 16=max_heart_rate, 17=avg_cadence, 18=max_cadence,
        # 19=avg_power, 20=max_power, 21=total_ascent, 22=total_descent,
        # 24=lap_trigger, 25=sport, 26=sub_sport, 33=normalized_power,
        # 34=left_right_balance,
        # 89=avg_power_position(uint16×2), 90=max_power_position(uint16×2),
        # 91-95=torque_eff/smoothness,
        # 96=avg_cadence_position(uint8×2), 97=max_cadence_position(uint8×2),
        # 98=time_standing, 99=stand_count,
        # 100=avg_left_pco, 101=avg_right_pco,
        # 102-105=power_phase(uint8×4 each: seated_start,seated_end,standing_start,standing_end)
        self._ensure_def("lap", _def_msg(self.LN_LAP, 19, [
            (253,4,0x86),(254,2,0x84),(0,1,0x00),(1,1,0x00),
            (2,4,0x86),(7,4,0x86),(8,4,0x86),(9,4,0x86),
            (11,2,0x84),  # total_calories
            (15,1,0x02),(16,1,0x02),(17,1,0x02),(18,1,0x02),
            (19,2,0x84),(20,2,0x84),(21,2,0x84),(22,2,0x84),(24,1,0x00),(25,1,0x00),(26,1,0x00),
            (33,2,0x84),  # normalized_power
            (34,2,0x84),  # left_right_balance
            (91,1,0x02),(92,1,0x02),(93,1,0x02),(94,1,0x02),(95,1,0x02),
            (98,4,0x86),(99,2,0x84),
            (100,1,0x01),(101,1,0x01),
            (102,4,0x02),(103,4,0x02),(104,4,0x02),(105,4,0x02),
            (106,4,0x84),  # avg_power_position (2×uint16)
            (107,4,0x84),  # max_power_position (2×uint16)
            (108,2,0x02),  # avg_cadence_position (2×uint8)
            (109,2,0x02),  # max_cadence_position (2×uint8)
        ]))
        elapsed_ms = int(elapsed * 1000)

        buf = (_data_hdr(self.LN_LAP)
            + _u32(ts_fit)                                          # 253 timestamp
            + _u16(message_index)                                    # 254 message_index
            + bytes([9])                                             # 0   event = lap
            + bytes([1])                                             # 1   event_type = stop
            + _u32(start_ts)                                         # 2   start_time
            + _u32(elapsed_ms) + _u32(elapsed_ms)                    # 7,8 elapsed/timer
            + _u32(int(distance * 100))                              # 9   total_distance
            + _u16(total_calories if total_calories else INVALID_U16) # 11 total_calories
            + _u8(avg_hr if avg_hr else INVALID_U8)                  # 15  avg_heart_rate
            + _u8(max_hr if max_hr else INVALID_U8)                  # 16  max_heart_rate
            + _u8(avg_cad if avg_cad else INVALID_U8)                # 17  avg_cadence
            + _u8(max_cad if max_cad else INVALID_U8)                # 18  max_cadence
            + _u16(avg_pow if avg_pow else INVALID_U16)              # 19  avg_power
            + _u16(max_pow if max_pow else INVALID_U16)              # 20  max_power
            + _u16(asc if asc else INVALID_U16)                      # 21  total_ascent
            + _u16(desc if desc else INVALID_U16)                    # 22  total_descent
            + _u8(lap_trigger)                                       # 24  lap_trigger
            + _u8(sport)                                             # 25  sport
            + _u8(sub_sport)                                         # 26  sub_sport
            + _u16(normalized_power if normalized_power else INVALID_U16)  # 33 normalized_power
            + _u16(left_right_balance if left_right_balance is not None else INVALID_U16)  # 34
            + _u8(max(0, min(254, int(round(avg_left_torque_eff  * 2)))) if avg_left_torque_eff  is not None else INVALID_U8)  # 91
            + _u8(max(0, min(254, int(round(avg_right_torque_eff * 2)))) if avg_right_torque_eff is not None else INVALID_U8)  # 92
            + _u8(max(0, min(254, int(round(avg_left_smoothness  * 2)))) if avg_left_smoothness  is not None else INVALID_U8)  # 93
            + _u8(max(0, min(254, int(round(avg_right_smoothness * 2)))) if avg_right_smoothness is not None else INVALID_U8)  # 94
            + _u8(max(0, min(254, int(round(avg_combined_smoothness * 2)))) if avg_combined_smoothness is not None else INVALID_U8)  # 95
            + _u32(int(time_standing * 1000) if time_standing is not None else INVALID_U32)  # 98 time_standing
            + _u16(stand_count if stand_count is not None else INVALID_U16)                  # 99 stand_count
            + (struct.pack('<b', max(-127, min(127, int(round(avg_left_pco)))))  if avg_left_pco  is not None else b'\x7f')  # 100
            + (struct.pack('<b', max(-127, min(127, int(round(avg_right_pco))))) if avg_right_pco is not None else b'\x7f')  # 101
            + _enc_pp4(avg_left_pp)       # 102
            + _enc_pp4(avg_left_pp_peak)  # 103
            + _enc_pp4(avg_right_pp)      # 104
            + _enc_pp4(avg_right_pp_peak) # 105
            + _enc_u16_pair(avg_power_position)                      # 106 avg_power_position
            + _enc_u16_pair(max_power_position)                      # 107 max_power_position
            + _enc_u8_pair(avg_cadence_position)                     # 108 avg_cadence_position
            + _enc_u8_pair(max_cadence_position)                     # 109 max_cadence_position
        )
        self._write(buf)

    def write_session(self, ts_fit, start_ts, elapsed, distance,
                      sport=2, sub_sport=0,
                      avg_hr=None, max_hr=None,
                      avg_cad=None, max_cad=None, avg_pow=None, max_pow=None,
                      asc=None, desc=None,
                      time_standing=None, stand_count=None,
                      avg_left_torque_eff=None, avg_right_torque_eff=None,
                      avg_left_smoothness=None, avg_right_smoothness=None,
                      avg_combined_smoothness=None,
                      avg_left_pco=None, avg_right_pco=None,
                      avg_left_pp=None, avg_left_pp_peak=None,
                      avg_right_pp=None, avg_right_pp_peak=None,
                      normalized_power=None, threshold_power=None,
                      intensity_factor=None, training_stress_score=None,
                      total_calories=None, total_work=None, total_cycles=None,
                      avg_temperature=None, left_right_balance=None,
                      avg_power_position=None, max_power_position=None,
                      avg_cadence_position=None, max_cadence_position=None,
                      total_training_effect=None, total_anaerobic_training_effect=None,
                      training_load_peak=None,
                      enhanced_avg_respiration_rate=None,
                      enhanced_max_respiration_rate=None,
                      enhanced_min_respiration_rate=None,
                      first_lap_index=0, num_laps=1):
        # FIT SDK profile for Session (mesg_num=18)
        # 24  = total_training_effect (uint8, ×10)
        # 137 = total_anaerobic_training_effect (uint8, ×10)
        # 120 = avg_power_position(uint16×2), 121=max_power_position(uint16×2)
        # 122 = avg_cadence_position(uint8×2), 123=max_cadence_position(uint8×2)
        # 116-119 = power_phase(uint8×4 each: seated+standing)
        # 168 = training_load_peak (sint32, ×65536)
        # 169 = enhanced_avg_respiration_rate (uint16, ×100)
        # 170 = enhanced_max_respiration_rate (uint16, ×100)
        # 180 = enhanced_min_respiration_rate (uint16, ×100)
        self._ensure_def("session", _def_msg(self.LN_SESSION, 18, [
            (253,4,0x86),(254,2,0x84),(0,1,0x00),(1,1,0x00),
            (2,4,0x86),(5,1,0x00),(6,1,0x00),
            (7,4,0x86),(8,4,0x86),(9,4,0x86),
            (10,4,0x86),  # total_cycles (uint32)
            (11,2,0x84),  # total_calories (uint16)
            (16,1,0x02),(17,1,0x02),(18,1,0x02),(19,1,0x02),  # avg/max HR, avg/max cadence
            (20,2,0x84),(21,2,0x84),(22,2,0x84),(23,2,0x84),  # avg/max power, ascent, descent
            (25,2,0x84),(26,2,0x84),(28,1,0x00),               # first_lap, num_laps, trigger
            (34,2,0x84),   # normalized_power
            (35,2,0x84),   # training_stress_score (×10)
            (36,2,0x84),   # intensity_factor (×1000)
            (37,2,0x84),   # left_right_balance
            (45,2,0x84),   # threshold_power (FTP)
            (48,4,0x86),   # total_work (joules, uint32)
            (57,1,0x01),   # avg_temperature (sint8)
            (24,1,0x02),   # total_training_effect (uint8, ×10)
            (137,1,0x02),  # total_anaerobic_training_effect (uint8, ×10)
            (101,1,0x02),(102,1,0x02),(103,1,0x02),(104,1,0x02),(105,1,0x02),
            (112,4,0x86),(113,2,0x84),
            (114,1,0x01),(115,1,0x01),
            (116,4,0x02),(117,4,0x02),(118,4,0x02),(119,4,0x02),
            (120,4,0x84),  # avg_power_position (2×uint16)
            (121,4,0x84),  # max_power_position (2×uint16)
            (122,2,0x02),  # avg_cadence_position (2×uint8)
            (123,2,0x02),  # max_cadence_position (2×uint8)
            (169,2,0x84),  # enhanced_avg_respiration_rate (uint16, ×100)
            (170,2,0x84),  # enhanced_max_respiration_rate (uint16, ×100)
            (180,2,0x84),  # enhanced_min_respiration_rate (uint16, ×100)
            (168,4,0x85),  # training_load_peak (sint32, ×65536)
        ]))
        elapsed_ms = int(elapsed * 1000)

        buf = (_data_hdr(self.LN_SESSION)
            + _u32(ts_fit)                                           # 253 timestamp
            + _u16(0)                                                # 254 message_index
            + bytes([0])                                             # 0   event = timer
            + bytes([4])                                             # 1   event_type = stop_all
            + _u32(start_ts)                                         # 2   start_time
            + _u8(sport)                                             # 5   sport
            + _u8(sub_sport)                                         # 6   sub_sport
            + _u32(elapsed_ms) + _u32(elapsed_ms)                    # 7,8 elapsed/timer
            + _u32(int(distance * 100))                              # 9   total_distance
            + _u32(total_cycles if total_cycles else INVALID_U32)    # 10  total_cycles
            + _u16(total_calories if total_calories else INVALID_U16) # 11 total_calories
            + _u8(avg_hr if avg_hr else INVALID_U8)                  # 16  avg_heart_rate
            + _u8(max_hr if max_hr else INVALID_U8)                  # 17  max_heart_rate
            + _u8(avg_cad if avg_cad else INVALID_U8)                # 18  avg_cadence
            + _u8(max_cad if max_cad else INVALID_U8)                # 19  max_cadence
            + _u16(avg_pow if avg_pow else INVALID_U16)              # 20  avg_power
            + _u16(max_pow if max_pow else INVALID_U16)              # 21  max_power
            + _u16(asc if asc else INVALID_U16)                      # 22  total_ascent
            + _u16(desc if desc else INVALID_U16)                    # 23  total_descent
            + _u16(first_lap_index)                                  # 25  first_lap_index
            + _u16(num_laps)                                         # 26  num_laps
            + bytes([4])                                             # 28  trigger = activity_end
            + _u16(normalized_power if normalized_power else INVALID_U16)  # 34 normalized_power
            + _u16(int(round(training_stress_score * 10)) if training_stress_score else INVALID_U16)  # 35 TSS (×10)
            + _u16(int(round(intensity_factor * 1000)) if intensity_factor else INVALID_U16)  # 36 IF (×1000)
            + _u16(left_right_balance if left_right_balance else INVALID_U16)  # 37 left_right_balance
            + _u16(threshold_power if threshold_power else INVALID_U16)    # 45 threshold_power (FTP)
            + _u32(total_work if total_work else INVALID_U32)        # 48 total_work (joules)
            + (struct.pack('<b', max(-127, min(127, int(avg_temperature)))) if avg_temperature is not None else b'\x7f')  # 57 avg_temperature
            + (_u8(max(0, min(254, int(round(total_training_effect * 10))))) if total_training_effect is not None else INVALID_U8)   # 24 total_training_effect (×10)
            + (_u8(max(0, min(254, int(round(total_anaerobic_training_effect * 10))))) if total_anaerobic_training_effect is not None else INVALID_U8)  # 137 anaerobic TE (×10)
            + _u8(max(0, min(254, int(round(avg_left_torque_eff  * 2)))) if avg_left_torque_eff  is not None else INVALID_U8)  # 101
            + _u8(max(0, min(254, int(round(avg_right_torque_eff * 2)))) if avg_right_torque_eff is not None else INVALID_U8)  # 102
            + _u8(max(0, min(254, int(round(avg_left_smoothness  * 2)))) if avg_left_smoothness  is not None else INVALID_U8)  # 103
            + _u8(max(0, min(254, int(round(avg_right_smoothness * 2)))) if avg_right_smoothness is not None else INVALID_U8)  # 104
            + _u8(max(0, min(254, int(round(avg_combined_smoothness * 2)))) if avg_combined_smoothness is not None else INVALID_U8)  # 105
            + _u32(int(time_standing * 1000) if time_standing is not None else INVALID_U32)  # 112 time_standing
            + _u16(stand_count if stand_count is not None else INVALID_U16)                  # 113 stand_count
            + (struct.pack('<b', max(-127, min(127, int(round(avg_left_pco)))))  if avg_left_pco  is not None else b'\x7f')  # 114
            + (struct.pack('<b', max(-127, min(127, int(round(avg_right_pco))))) if avg_right_pco is not None else b'\x7f')  # 115
            + _enc_pp4(avg_left_pp)       # 116
            + _enc_pp4(avg_left_pp_peak)  # 117
            + _enc_pp4(avg_right_pp)      # 118
            + _enc_pp4(avg_right_pp_peak) # 119
            + _enc_u16_pair(avg_power_position)   # 120
            + _enc_u16_pair(max_power_position)   # 121
            + _enc_u8_pair(avg_cadence_position)  # 122
            + _enc_u8_pair(max_cadence_position)  # 123
            + (_u16(max(0, min(65534, int(round(enhanced_avg_respiration_rate * 100))))) if enhanced_avg_respiration_rate is not None else INVALID_U16)  # 169
            + (_u16(max(0, min(65534, int(round(enhanced_max_respiration_rate * 100))))) if enhanced_max_respiration_rate is not None else INVALID_U16)  # 170
            + (_u16(max(0, min(65534, int(round(enhanced_min_respiration_rate * 100))))) if enhanced_min_respiration_rate is not None else INVALID_U16)  # 180
            + (_s32(int(round(float(training_load_peak) * 65536))) if training_load_peak is not None else _s32(INVALID_S32))  # 168 training_load_peak (sint32, ×65536)
        )
        self._write(buf)

    def write_activity(self, ts_fit, local_ts_fit, elapsed):
        # FIT SDK profile for Activity (mesg_num=34):
        # 253=timestamp(uint32), 0=total_timer_time(uint32,×1000),
        # 1=num_sessions(uint16), 2=type(activity enum), 3=event(enum),
        # 4=event_type(enum), 5=local_timestamp(uint32)
        self._ensure_def("activity", _def_msg(self.LN_ACTIVITY, 34, [
            (253,4,0x86),(0,4,0x86),(1,2,0x84),(2,1,0x00),
            (3,1,0x00),(4,1,0x00),(5,4,0x86),
        ]))
        buf = (_data_hdr(self.LN_ACTIVITY)
            + _u32(ts_fit)                    # 253 timestamp
            + _u32(int(elapsed * 1000))       # 0   total_timer_time (ms)
            + _u16(1)                         # 1   num_sessions
            + bytes([0])                      # 2   type = manual
            + bytes([26])                     # 3   event = activity
            + bytes([1])                      # 4   event_type = stop
            + _u32(local_ts_fit))             # 5   local_timestamp
        self._write(buf)

    def build(self):
        """Assemble and return the complete FIT file bytes."""
        body = bytes(self._body)
        # 12-byte header (no header CRC)
        data_size = len(body)
        header = (bytes([12, 0x10, 0x70, 0x08])   # header_size, protocol, profile (2.14)
                  + struct.pack('<I', data_size)
                  + b'.FIT')
        full = header + body
        file_crc = _crc(full)
        return full + struct.pack('<H', file_crc)

