A7.py     SSIv2.py  SSIv4.py  SSIv6.py  SSIv8.py  Z19.py            pmu_3_worker.log
SSIv1.py  SSIv3.py  SSIv5.py  SSIv7.py  SSIv9.py  pmu_1_worker.log
root@5GB:~/20251219# cat A7.py
# A7.py
# Catatan: Kode ini secara fungsional identik dengan A6.py,
# karena A6.py sudah dikonfigurasi untuk HANYA menggunakan PMU 1 dan PMU 3.

import time
import collections
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS
from phasortoolbox import Client
import logging
from multiprocessing import Process, Queue, Event
import sys
import os
from dataclasses import dataclass
import traceback
import threading
from typing import Dict, Deque, Optional, List, Tuple
import math  # <-- Ditambahkan untuk perhitungan daya

# --- Konfigurasi ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger("phasortoolbox.client").setLevel(logging.CRITICAL)

# Konfigurasi PMU (sesuai dengan perangkat Anda)
PMU_CONFIGS = [
    {'name': 'PMU 1', 'ip': '172.17.3.56', 'port': 4712, 'id': 2},
    {'name': 'PMU 3', 'ip': '172.17.28.131', 'port': 4712, 'id': 1},
]

# Konfigurasi InfluxDB
INFLUXDB_CONFIG = {
    'url': 'http://172.17.3.223:8086',
    'token': 'Wo8Rc63Bep01wXaBlM06TgN0TGOvJw4ygVa39dRgcGqwmPs0-aV4aZIv4191xdYJLsjlV2MZvSunX21uU8xITg==',
    'org': 'PLN',
    'bucket': 'pmu_synced_raw',
    'measurement': 'pmu_synced_opt'
}

# --- Parameter Operasional ---
TARGET_FPS = 25
TIME_TOLERANCE = 0.001
REPORT_INTERVAL = 1.0
FRAME_INTERVAL_S = 1.0 / TARGET_FPS
FRAME_INTERVAL_NS = int(FRAME_INTERVAL_S * 1_000_000_000)
BUFFER_MAX_LEN = 200
PERFORMANCE_MONITOR_INTERVAL = 5.0
MIN_ACCEPTABLE_FPS = TARGET_FPS * 0.6
PMU_STALE_TIMEOUT_S = 3.0
INITIAL_RECONNECT_DELAY = 2.0
MAX_RECONNECT_DELAY = 60.0
RESTART_TIMEOUT_S = 20
LOW_FPS_THRESHOLD = 10.0
LOW_FPS_DURATION_S = 30.0
INITIAL_STABILITY_PERIOD_S = 20.0

@dataclass
class SyncedPoint:
    timestamp_ns: int
    fields: dict

@dataclass
class PerformanceMetrics:
    current_fps: float = 0.0
    avg_buffer_size: float = 0.0
    queue_size: int = 0
    consecutive_low_fps: int = 0
    last_cleanup_time: float = 0.0

class PerformanceMonitor:
    def __init__(self):
        self.metrics = PerformanceMetrics()
        self.fps_history = collections.deque(maxlen=10)
        self.last_saved_count = 0
        self.last_check_time = time.time()

    def update_metrics(self, total_saved: int, buffer_sizes: list, queue_size: int):
        current_time = time.time()
        elapsed = current_time - self.last_check_time
        if elapsed >= 1.0:
            points_in_last_second = total_saved - self.last_saved_count
            current_fps = points_in_last_second / elapsed
            self.fps_history.append(current_fps)
            self.metrics.current_fps = current_fps
            self.metrics.avg_buffer_size = sum(buffer_sizes) / len(buffer_sizes) if buffer_sizes else 0
            self.metrics.queue_size = queue_size
            if current_fps < MIN_ACCEPTABLE_FPS:
                self.metrics.consecutive_low_fps += 1
            else:
                self.metrics.consecutive_low_fps = 0
            self.last_saved_count = total_saved
            self.last_check_time = current_time

    def get_avg_fps(self) -> float:
        return sum(self.fps_history) / len(self.fps_history) if self.fps_history else 0.0

class BufferManager:
    def __init__(self, pmu_configs: list):
        self.buffers: Dict[str, Deque] = {c['name']: collections.deque(maxlen=BUFFER_MAX_LEN) for c in pmu_configs}

    def add_data(self, pmu_name: str, data):
        if pmu_name in self.buffers:
            self.buffers[pmu_name].append(data)

    def get_buffer_sizes(self) -> list:
        return [len(self.buffers[pmu['name']]) for pmu in PMU_CONFIGS]

    def can_sync(self, online_pmus: List[str]) -> bool:
        if not online_pmus:
            return False
        return len(online_pmus) == len(PMU_CONFIGS) and all(len(self.buffers[name]) > 0 for name in online_pmus)

    def get_first_messages(self, pmu_names: List[str]) -> dict:
        return {name: self.buffers[name][0] for name in pmu_names if self.buffers.get(name)}

    def pop_first(self, pmu_names: List[str]):
        for name in pmu_names:
            if self.buffers.get(name) and self.buffers[name]:
                self.buffers[name].popleft()

def setup_worker_logging(name: str):
    log_file_name = f"{name.replace(' ', '_').lower()}_worker.log"
    handler = logging.FileHandler(log_file_name, mode='w')
    formatter = logging.Formatter('%(asctime)s - %(process)d - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
    return logger

# =================================================================================
# --- FUNGSI PMU_WORKER DENGAN SHUTDOWN BERSIH ---
# =================================================================================
def pmu_worker(pmu_config: dict, data_queue: Queue, stop_event: Event):
    pmu_name = pmu_config['name']
    logger = setup_worker_logging(pmu_name)
    reconnect_delay = INITIAL_RECONNECT_DELAY

    def callback(msg):
        if stop_event.is_set(): return
        if data_queue.full():
            try: data_queue.get_nowait()
            except: pass
        try: data_queue.put((pmu_name, msg), timeout=0.1)
        except: pass

    try:
        while not stop_event.is_set():
            client_thread = None
            try:
                client = Client(remote_ip=pmu_config['ip'], remote_port=pmu_config['port'], idcode=pmu_config['id'], mode='TCP')
                client.callback = callback
                logger.info(f"Berhasil terhubung ke {pmu_config['ip']}:{pmu_config['port']}")
                reconnect_delay = INITIAL_RECONNECT_DELAY
                
                client_thread = threading.Thread(target=client.run, daemon=True)
                client_thread.start()

                while not stop_event.is_set() and client_thread.is_alive():
                    time.sleep(0.2) 

                if not stop_event.is_set():
                    logger.warning("Koneksi terputus secara normal. Mencoba terhubung kembali...")

            except Exception as e:
                if not stop_event.is_set():
                    logger.error(f"Gagal terhubung: {e}. Mencoba lagi dalam {reconnect_delay:.1f} detik...", exc_info=False)
                    if stop_event.wait(timeout=reconnect_delay): break
                    reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)
            
            finally:
                if stop_event.is_set():
                        logger.info("Sinyal stop diterima, worker akan berhenti...")

    except KeyboardInterrupt:
        pass


def shutdown_and_cleanup(stop_event, processes, write_api, influx_client, points_batch):
    logging.info("--- Memulai Prosedur Shutdown Terkendali ---")
    stop_event.set()
    for p in processes:
        if p.is_alive():
            p.join(timeout=3)
        if p.is_alive():
            logging.warning(f"Worker {p.name} tidak berhenti, terminasi paksa.")
            p.terminate()
            p.join()
    if write_api and points_batch:
        logging.info(f"Flushing final {len(points_batch)} points to InfluxDB...")
        try:
            final_write_api = influx_client.write_api()
            final_write_api.write(bucket=INFLUXDB_CONFIG['bucket'], org=INFLUXDB_CONFIG['org'], record=points_batch)
            logging.info(f"Final flush: {len(points_batch)} points successful.")
        except Exception as e:
            logging.error(f"Final flush error: {e}")
    if write_api:
        write_api.close()
    if influx_client:
        influx_client.close()
    logging.info("--- Prosedur Shutdown Selesai ---")

def main():
    logging.info("--- PMU Data Synchronizer (Shutdown Bersih) ---")
    start_time = time.time()
    
    data_queue=Queue(maxsize=8000)
    stop_event=Event()
    processes=[]
    for cfg in PMU_CONFIGS: 
        p=Process(target=pmu_worker,args=(cfg,data_queue,stop_event),daemon=True,name=cfg['name'])
        processes.append(p)
        p.start()
    
    buffer_manager=BufferManager(PMU_CONFIGS)
    performance_monitor=PerformanceMonitor()
    points_batch=[]
    write_api=None
    influx_client=None
    
    total_saved, total_discarded_resync = 0, 0
    last_report_time = time.time()
    pmu_status={p['name']:'INIT' for p in PMU_CONFIGS}
    last_data_timestamp={p['name']:time.time() for p in PMU_CONFIGS}
    imperfect_state_since=None
    low_fps_since = None

    def update_and_report_status(current_time, online_pmus):
        nonlocal last_report_time, total_saved, points_batch

        if (current_time - last_report_time) >= REPORT_INTERVAL:
            if points_batch:
                try:
                    write_api.write(bucket=INFLUXDB_CONFIG['bucket'], org=INFLUXDB_CONFIG['org'], record=points_batch)
                    total_saved += len(points_batch)
                    points_batch.clear()
                except Exception as e:
                    logging.error(f"InfluxDB Write error: {e}")
            
            buffer_sizes = buffer_manager.get_buffer_sizes()
            performance_monitor.update_metrics(total_saved, buffer_sizes, data_queue.qsize())
            
            status_char={'ONLINE':'O','RECONNECTING':'R','INIT':'I'}
            status_str=''.join([status_char.get(pmu_status.get(p['name']),'?') for p in PMU_CONFIGS])
            
            is_perfect=len(online_pmus)==len(PMU_CONFIGS)
            fps_label="FPS (Sync)" if is_perfect else "FPS (Idle)"
                    
            status_line=(f"\r{fps_label:<12}: {performance_monitor.metrics.current_fps:5.1f}/{TARGET_FPS} ({performance_monitor.get_avg_fps():4.1f}avg) | "
                         f"Saved: {total_saved:8d} | Discard: {total_discarded_resync:6d} | "
                         f"Q: {data_queue.qsize():<5d} | Buf: {str(buffer_sizes):<12} | Status: {status_str}")
            print(status_line, end="", flush=True)
            last_report_time = current_time

    try:
        influx_client = InfluxDBClient(url=INFLUXDB_CONFIG['url'], token=INFLUXDB_CONFIG['token'], org=INFLUXDB_CONFIG['org'])
        write_api = influx_client.write_api(write_options=ASYNCHRONOUS)
        logging.info("Koneksi InfluxDB berhasil.")
        logging.info("Memulai pemrosesan data... Tekan CTRL+C untuk berhenti.")

        while not stop_event.is_set():
            current_time = time.time()
            
            batch_moved=0
            while not data_queue.empty() and batch_moved < 100:
                try:
                    pmu_name,msg=data_queue.get_nowait()
                    buffer_manager.add_data(pmu_name,msg)
                    last_data_timestamp[pmu_name]=current_time
                    if pmu_status[pmu_name]!='ONLINE':
                        print(f"\n[+] PMU {pmu_name} telah KEMBALI ONLINE.")
                        pmu_status[pmu_name]='ONLINE'
                    batch_moved+=1
                except: 
                    break
            
            online_pmus=[]
            for name in pmu_status.keys():
                if pmu_status[name]=='ONLINE' and (current_time-last_data_timestamp[name]) > PMU_STALE_TIMEOUT_S:
                    pmu_status[name]='RECONNECTING'
                    print(f"\n[!] Koneksi PMU {name} hilang. Memulai mode RECONNECTING...")
                if pmu_status[name]=='ONLINE':
                    online_pmus.append(name)
            
            is_perfect_state = len(online_pmus) == len(PMU_CONFIGS)
            if not is_perfect_state:
                if imperfect_state_since is None:
                    imperfect_state_since = current_time
            else:
                if imperfect_state_since is not None:
                    print("\n[+] Sistem kembali ke kondisi sempurna (semua PMU online).")
                imperfect_state_since = None

            avg_fps = performance_monitor.get_avg_fps()
            if (current_time - start_time) > INITIAL_STABILITY_PERIOD_S:
                if avg_fps < LOW_FPS_THRESHOLD and is_perfect_state and low_fps_since is None:
                    low_fps_since = current_time
                    print(f"\n[!!!] PERINGATAN: FPS rata-rata ({avg_fps:.1f}) di bawah ambang batas ({LOW_FPS_THRESHOLD:.1f}). Memulai timer restart {LOW_FPS_DURATION_S} detik...")
                elif low_fps_since is not None and avg_fps >= LOW_FPS_THRESHOLD:
                    print(f"\n[+] INFO: FPS rata-rata ({avg_fps:.1f}) telah pulih. Timer restart dibatalkan.")
                    low_fps_since = None
            
            trigger_restart = False
            restart_reason = ""
            if imperfect_state_since is not None and (current_time - imperfect_state_since) > RESTART_TIMEOUT_S:
                trigger_restart = True
                restart_reason = f"sistem tidak pulih dari status PMU offline selama {RESTART_TIMEOUT_S} detik"
            elif low_fps_since is not None and (current_time - low_fps_since) > LOW_FPS_DURATION_S:
                trigger_restart = True
                restart_reason = f"FPS rata-rata tetap rendah ({avg_fps:.1f} FPS) selama lebih dari {LOW_FPS_DURATION_S} detik"

            if trigger_restart:
                print(f"\n[!!!] PENYEBAB RESTART: {restart_reason}.")
                shutdown_and_cleanup(stop_event, processes, write_api, influx_client, points_batch)
                print(f"[*] Beristirahat selama 5 detik sebelum restart...")
                time.sleep(5)
                print("[*] Merestart aplikasi...")
                os.execv(sys.executable, [sys.executable] + sys.argv)
                break 

            if buffer_manager.can_sync(online_pmus):
                try:
                    current_msgs = buffer_manager.get_first_messages(online_pmus)
                    messages = list(current_msgs.values())
                    min_msg_time=min(m.time for m in messages)
                    max_msg_time=max(m.time for m in messages)

                    if (max_msg_time-min_msg_time) <= TIME_TOLERANCE:
                        rounded_timestamp_ns=int(round(max_msg_time*1e9/FRAME_INTERVAL_NS)*FRAME_INTERVAL_NS)
                        current_fields={}
                        for p_config in PMU_CONFIGS:
                            p_name=p_config['name']
                            p_name_field=p_name.replace(" ","_")
                            p_msg=current_msgs[p_name]
                            
                            current_fields[f'freq_{p_name_field}']=float(p_msg.data.pmu_data[0].freq)
                            
                            angle_value = 0.0
                            try:
                                if p_name == 'PMU 3':
                                    angle_value = p_msg.data.pmu_data[0].phasors[1].angle
                                    
                                    # --- START: Perhitungan totw_langsa ---
                                    try:
                                        pmu_data = p_msg.data.pmu_data[0]
                                        phasors = pmu_data.phasors

                                        # Ekstrak magnitudo dan sudut (sudah dalam radian)
                                        VR, angle_VR = phasors[0].magnitude, phasors[0].angle
                                        VS, angle_VS = phasors[1].magnitude, phasors[1].angle
                                        VT, angle_VT = phasors[2].magnitude, phasors[2].angle
                                        IR, angle_IR = phasors[6].magnitude, phasors[6].angle
                                        IS, angle_IS = phasors[7].magnitude, phasors[7].angle
                                        IT, angle_IT = phasors[8].magnitude, phasors[8].angle

                                        # Hitung daya aktif per fasa
                                        power_R = VR * IR * math.cos(angle_VR - angle_IR)
                                        power_S = VS * IS * math.cos(angle_VS - angle_IS)
                                        power_T = VT * IT * math.cos(angle_VT - angle_IT)

                                        # Jumlahkan untuk mendapatkan daya total
                                        totw_langsa = power_R + power_S + power_T
                                        
                                        current_fields['totw_langsa'] = float(totw_langsa)

                                    except IndexError:
                                        logging.warning(f"\nIndexError saat menghitung totw_langsa untuk {p_name}. Pastikan data phasor (0,1,2,6,7,8) ada.")
                                        current_fields['totw_langsa'] = 0.0 # Nilai default jika gagal
                                    except Exception as calc_e:
                                        logging.error(f"\nError saat kalkulasi totw_langsa: {calc_e}")
                                        current_fields['totw_langsa'] = 0.0 # Nilai default jika gagal
                                    # --- END: Perhitungan totw_langsa ---
                                else:
                                    angle_value = p_msg.data.pmu_data[0].phasors[0].angle
                                
                                current_fields[f'angle_{p_name_field}'] = float(angle_value)
                            except IndexError:
                                logging.warning(f"\nIndexError saat mengambil sudut untuk {p_name}. Memastikan data phasor yang diminta ada.")
                                current_fields[f'angle_{p_name_field}'] = 0.0
                        
                        real_point=Point(INFLUXDB_CONFIG['measurement']).time(rounded_timestamp_ns).tag("type","real")
                        for key,val in current_fields.items():
                            real_point.field(key,val)
                        points_batch.append(real_point)
                        buffer_manager.pop_first(online_pmus)
                    else:
                        max_of_firsts=max(buf[0].time for name in online_pmus for buf in[buffer_manager.buffers[name]]if buf)
                        discarded_count=0
                        for name in online_pmus:
                            buffer=buffer_manager.buffers[name]
                            while buffer and buffer[0].time<max_of_firsts:
                                buffer.popleft()
                                discarded_count+=1
                        total_discarded_resync+=discarded_count
                except Exception as e:
                    logging.error(f"Data processing error: {e}",exc_info=True)
                    buffer_manager.pop_first(online_pmus)
            
            update_and_report_status(current_time, online_pmus)
            
            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\n\n[!] Sinyal shutdown diterima (CTRL+C)...")
    except Exception as e:
        logging.critical(f"Terjadi error fatal di loop utama: {e}", exc_info=True)
    finally:
        shutdown_and_cleanup(stop_event, processes, write_api, influx_client, points_batch)
        logging.info("Program shutdown selesai.")

if __name__ == '__main__':
    main()
