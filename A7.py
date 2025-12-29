# A7.py
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
import math

# --- Konfigurasi ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger("phasortoolbox.client").setLevel(logging.CRITICAL)

PMU_CONFIGS = [
    {'name': 'PMU 1', 'ip': '172.17.3.56', 'port': 4712, 'id': 2},
    {'name': 'PMU 3', 'ip': '172.17.28.131', 'port': 4712, 'id': 1},
]

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
FRAME_INTERVAL_S = 1.0 / TARGET_FPS
FRAME_INTERVAL_NS = int(FRAME_INTERVAL_S * 1_000_000_000)
BUFFER_MAX_LEN = 200
MIN_ACCEPTABLE_FPS = TARGET_FPS * 0.6
PMU_STALE_TIMEOUT_S = 3.0
INITIAL_RECONNECT_DELAY = 2.0
MAX_RECONNECT_DELAY = 60.0
RESTART_TIMEOUT_S = 20
LOW_FPS_THRESHOLD = 10.0
LOW_FPS_DURATION_S = 30.0
INITIAL_STABILITY_PERIOD_S = 20.0
REPORT_INTERVAL = 1.0

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

    # METODE YANG DIPERBAIKI (Ditambahkan kembali)
    def get_buffer_sizes(self) -> list:
        return [len(self.buffers[pmu['name']]) for pmu in PMU_CONFIGS]

    def can_sync(self, online_pmus: List[str]) -> bool:
        if not online_pmus: return False
        return len(online_pmus) == len(PMU_CONFIGS) and all(len(self.buffers[name]) > 0 for name in online_pmus)

    def get_first_messages(self, pmu_names: List[str]) -> dict:
        return {name: self.buffers[name][0] for name in pmu_names if self.buffers.get(name)}

    def pop_first(self, pmu_names: List[str]):
        for name in pmu_names:
            if self.buffers.get(name) and self.buffers[name]:
                self.buffers[name].popleft()

def calculate_active_power(pmu_data) -> float:
    """Menghitung total daya aktif 3-fasa"""
    try:
        phasors = pmu_data.phasors
        P_total = 0.0
        # Menghitung P = V * I * cos(theta_v - theta_i) untuk 3 fasa
        for i in range(3):
            v_mag, v_ang = phasors[i].magnitude, phasors[i].angle
            i_mag, i_ang = phasors[i+6].magnitude, phasors[i+6].angle
            P_total += v_mag * i_mag * math.cos(v_ang - i_ang)
        return P_total
    except (IndexError, AttributeError):
        return 0.0

def pmu_worker(pmu_config: dict, data_queue: Queue, stop_event: Event):
    pmu_name = pmu_config['name']
    reconnect_delay = INITIAL_RECONNECT_DELAY
    def callback(msg):
        if stop_event.is_set(): return
        if data_queue.full():
            try: data_queue.get_nowait()
            except: pass
        try: data_queue.put((pmu_name, msg), timeout=0.1)
        except: pass

    while not stop_event.is_set():
        try:
            client = Client(remote_ip=pmu_config['ip'], remote_port=pmu_config['port'], idcode=pmu_config['id'], mode='TCP')
            client.callback = callback
            logging.info(f"Terhubung ke {pmu_name}")
            client.run()
        except Exception:
            if not stop_event.is_set():
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

def shutdown_and_cleanup(stop_event, processes, write_api, influx_client, points_batch):
    logging.info("--- Memulai Shutdown ---")
    stop_event.set()
    for p in processes:
        p.join(timeout=2)
        if p.is_alive(): p.terminate()
    if write_api and points_batch:
        try: write_api.write(bucket=INFLUXDB_CONFIG['bucket'], org=INFLUXDB_CONFIG['org'], record=points_batch)
        except: pass
    if influx_client: influx_client.close()

def main():
    data_queue = Queue(maxsize=8000)
    stop_event = Event()
    processes = []
    for cfg in PMU_CONFIGS:
        p = Process(target=pmu_worker, args=(cfg, data_queue, stop_event), daemon=True, name=cfg['name'])
        processes.append(p)
        p.start()

    influx_client = InfluxDBClient(url=INFLUXDB_CONFIG['url'], token=INFLUXDB_CONFIG['token'], org=INFLUXDB_CONFIG['org'])
    write_api = influx_client.write_api(write_options=ASYNCHRONOUS)
    buffer_manager = BufferManager(PMU_CONFIGS)
    perf_mon = PerformanceMonitor()
    
    total_saved, total_discarded = 0, 0
    last_report_time = time.time()
    pmu_status = {p['name']: 'INIT' for p in PMU_CONFIGS}
    last_data_ts = {p['name']: time.time() for p in PMU_CONFIGS}
    points_batch = []

    try:
        while not stop_event.is_set():
            curr_t = time.time()
            
            while not data_queue.empty():
                try:
                    name, msg = data_queue.get_nowait()
                    buffer_manager.add_data(name, msg)
                    last_data_ts[name] = curr_t
                    pmu_status[name] = 'ONLINE'
                except: break

            online_pmus = [n for n, s in pmu_status.items() if s == 'ONLINE' and (curr_t - last_data_ts[n]) < PMU_STALE_TIMEOUT_S]
            
            if buffer_manager.can_sync(online_pmus):
                msgs = buffer_manager.get_first_messages(online_pmus)
                times = [m.time for m in msgs.values()]
                
                if (max(times) - min(times)) <= TIME_TOLERANCE:
                    ts_ns = int(round(max(times)*1e9/FRAME_INTERVAL_NS)*FRAME_INTERVAL_NS)
                    point = Point(INFLUXDB_CONFIG['measurement']).time(ts_ns)
                    
                    for name in online_pmus:
                        p_data = msgs[name].data.pmu_data[0]
                        tag = name.replace(" ", "_")
                        
                        # Simpan Freq
                        point.field(f"freq_{tag}", float(p_data.freq))
                        
                        # Simpan Angle (PMU 3 index 1, PMU 1 index 0 sesuai mapping alat Anda)
                        ang_idx = 0 if name == 'PMU 3' else 0
                        point.field(f"angle_{tag}", float(p_data.phasors[ang_idx].angle))
                        
                        # Hitung dan Simpan Daya Aktif untuk KEDUA PMU
                        p_val = calculate_active_power(p_data)
                        point.field(f"totw_{tag.lower()}", float(p_val))
                    
                    points_batch.append(point)
                    buffer_manager.pop_first(online_pmus)
                else:
                    max_t = max(times)
                    for name in online_pmus:
                        buf = buffer_manager.buffers[name]
                        while buf and buf[0].time < max_t:
                            buf.popleft()
                            total_discarded += 1

            if (curr_t - last_report_time) >= REPORT_INTERVAL:
                if points_batch:
                    write_api.write(bucket=INFLUXDB_CONFIG['bucket'], record=points_batch)
                    total_saved += len(points_batch)
                    points_batch.clear()
                
                # Pemanggilan get_buffer_sizes sekarang aman
                perf_mon.update_metrics(total_saved, buffer_manager.get_buffer_sizes(), data_queue.qsize())
                print(f"\rFPS: {perf_mon.metrics.current_fps:4.1f} | Saved: {total_saved} | Status: {online_pmus}", end="")
                last_report_time = curr_t
            
            time.sleep(0.01)

    except KeyboardInterrupt: pass
    finally: shutdown_and_cleanup(stop_event, processes, write_api, influx_client, points_batch)

if __name__ == '__main__':
    main()
