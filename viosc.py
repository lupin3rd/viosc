import os
import re
import shutil
import subprocess
import sys
import threading
import time
import json
import random
from urllib.parse import unquote, urlparse

try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import BlockingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient
except ImportError:
    print("ERROR: the 'python-osc' library is not installed.")
    print("Install it with: pip install python-osc")
    sys.exit(1)

LISTEN_IP = "0.0.0.0"       
LISTEN_PORT = 6666          
LOCAL_BIND_IP = "127.0.0.1" 
FROMVIMIX_PORT = 7001       

UI_IP = os.environ.get("VIOSC_UI_IP", "127.0.0.1")   
REPLY_PORT = 6667           

TOVIMIX_IP = "127.0.0.1"
TOVIMIX_PORT = 7000

FFMPEG_PATH = os.environ.get("VIOSC_FFMPEG", "ffmpeg")
FFPROBE_PATH = os.environ.get("VIOSC_FFPROBE", "ffprobe")

LOG_LEVEL = 1

sync_interval_time = 2000    

ALL_PROPERTIES = [
    "index", "name", "lock", "failed", "play", "pause", "blending", "alpha",
    "transparency", "depth", "position", "size", "corner", "angle",
    "seek", "speed", "brightness", "contrast", "saturation", "hue",
    "threshold", "gamma", "color", "posterize", "invert", "uri"
]

SUPPORTED_PROPERTIES = set(ALL_PROPERTIES)

COLOR_RESET = "\033[0m"
COLOR_THUMB = "\033[96m"    
COLOR_RESET_EV = "\033[91m" 
COLOR_TIMESTAMP = "\033[90m"

forward_client = SimpleUDPClient(TOVIMIX_IP, TOVIMIX_PORT)
ui_reply_client = SimpleUDPClient(UI_IP, REPLY_PORT)

vimix_data = {}         
source_current = None   
seen_indices = set()    
sync_round = 0          
prune_timer = None      
PRUNE_DELAY_SEC = 0.5   
known_indices = set()   
monitored_sources = {}  
monitor_misses = {}     
MONITOR_MAX_MISSES = 3  
THUMB_MAX_CONCURRENCY = 3
thumb_semaphore = threading.BoundedSemaphore(THUMB_MAX_CONCURRENCY)

def create_empty_vimix_entry():
    entry = {prop: None for prop in SUPPORTED_PROPERTIES}
    entry["thumbnails"] = []
    return entry

def find_index_by_name(name_str):
    for idx, data in vimix_data.items():
        if data.get("name") == name_str: return idx
    return None

def clean_uri_path(uri_value):
    if not uri_value: return ""
    uri_str = str(uri_value).strip()
    if uri_str.startswith("file://"):
        raw_path = urlparse(uri_str).path
        clean_path = unquote(raw_path)
    else:
        clean_path = unquote(uri_str)
    return os.path.abspath(clean_path)

def get_video_duration(file_path):
    cmd = [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration:stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip() and line.strip() != "N/A"]
        for line in lines:
            try:
                val = float(line)
                if val > 0: return val
            except ValueError: continue
        return 0.0
    except Exception:
        return 0.0

def extract_single_frame(file_path, timestamp_sec, width=320, height=180):
    cmd = [
        FFMPEG_PATH, "-y", "-ss", f"{timestamp_sec:.2f}", "-accurate_seek",
        "-i", file_path, "-vframes", "1", "-s", f"{width}x{height}",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-"  
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0 and len(result.stdout) > 100:
            return result.stdout
        return None
    except Exception as e:
        print(f"{COLOR_RESET_EV}[THUMBNAIL ERROR]{COLOR_RESET} Unable to extract frame: {e}")
        return None

def extract_thumbnails_from_file(file_path, count=1, width=320, height=180):
    duration = get_video_duration(file_path)
    thumbnails = []
    if duration <= 0:
        frame = extract_single_frame(file_path, timestamp_sec=0, width=width, height=height)
        if frame: thumbnails.append(frame)
        return thumbnails

    target_time = random.uniform(0.1, 0.9) * duration
    frame = extract_single_frame(file_path, timestamp_sec=target_time, width=width, height=height)
    if frame: thumbnails.append(frame)
    return thumbnails

def generate_thumbnails_worker(idx, uri_value):
    file_path = clean_uri_path(uri_value)
    if not os.path.exists(file_path):
        if idx in vimix_data: vimix_data[idx]["thumbnails"] = []
        return

    with thumb_semaphore:
        thumbnails = extract_thumbnails_from_file(file_path, count=1)
    if idx in vimix_data:
        vimix_data[idx]["thumbnails"] = thumbnails
        if LOG_LEVEL >= 1:
            print(f"{COLOR_THUMB}[THUMB READY]{COLOR_RESET} Index {idx}: generated 1 random thumbnail for '{os.path.basename(file_path)}'")

def broadcast_vimix_state():
    safe_data = {idx: {k: v for k, v in data.items() if k != "thumbnails"} for idx, data in vimix_data.items()}
    payload = {"current_source": source_current, "sources": safe_data, "monitored": dict(monitored_sources)}
    try:
        ui_reply_client.send_message("/viosc/replydata", [json.dumps(payload)])
    except Exception as e: print(f"Broadcast error: {e}")


def start_new_sync_round():
    global sync_round, prune_timer
    sync_round += 1
    seen_indices.clear()
    if prune_timer:
        prune_timer.cancel()
    prune_timer = threading.Timer(PRUNE_DELAY_SEC, prune_stale_sources, args=(sync_round,))
    prune_timer.daemon = True
    prune_timer.start()


def prune_stale_sources(round_id):
    global vimix_data, source_current
    if round_id != sync_round:
        return
    stale = [idx for idx in list(vimix_data.keys()) if idx not in seen_indices]
    if not stale:
        return
    for idx in stale:
        del vimix_data[idx]
        known_indices.discard(idx)
        if LOG_LEVEL >= 1:
            print(f"{COLOR_TIMESTAMP}[PRUNE]{COLOR_RESET} Source removed: index {idx}")
    if not vimix_data:
        source_current = None
    broadcast_vimix_state()

def send_thumbnail_blob(target_identifier, thumb_arg):
    idx = int(target_identifier) if str(target_identifier).isdigit() else find_index_by_name(str(target_identifier))
    if idx is None or idx not in vimix_data: return
    
    thumbnails = vimix_data[idx].get("thumbnails", [])
    if not thumbnails: return

    arg_str = str(thumb_arg).lower().strip()
    if arg_str == "all": indices_to_send = list(range(len(thumbnails)))
    elif arg_str.isdigit() and 0 <= int(arg_str) < len(thumbnails): indices_to_send = [int(arg_str)]
    else: return

    for t_idx in indices_to_send:
        blob_data = thumbnails[t_idx]
        osc_reply_path = f"/viosc/replythumb/{target_identifier}/{t_idx}"
        ui_reply_client.send_message(osc_reply_path, blob_data)

def request_source_props(target_str, props):
    try:
        forward_client.send_message(f"/vimix/{target_str}/get", list(props))
    except Exception as e:
        print(f"Error sending get request for '{target_str}': {e}")

def request_uri_once(name_str):
    if not name_str:
        return
    request_source_props(name_str, ["uri"])

def process_dynamic_sync_request(clean_address, args):
    target = clean_address.replace("viosc/sync/", "", 1)
    req_args = [str(arg) for arg in args]
    props_to_send = ALL_PROPERTIES if not req_args or "all" in [a.lower() for a in req_args] else req_args
    request_source_props(target, props_to_send)

def process_monitor_command(target_identifier, args):
    name = str(target_identifier)
    if name.isdigit():
        cached_name = vimix_data.get(int(name), {}).get("name")
        if cached_name is not None:
            name = str(cached_name)
    props = [str(a) for a in args if str(a).strip()]
    if not props:
        if name in monitored_sources:
            del monitored_sources[name]
            monitor_misses.pop(name, None)
            if LOG_LEVEL >= 1:
                print(f"{COLOR_TIMESTAMP}[MONITOR]{COLOR_RESET} stopped monitoring: '{name}'")
            broadcast_vimix_state()
        return
    monitored_sources[name] = props
    monitor_misses.pop(name, None)
    if LOG_LEVEL >= 1:
        print(f"{COLOR_TIMESTAMP}[MONITOR]{COLOR_RESET} monitoring '{name}' -> {props}")
    request_source_props(name, props)
    broadcast_vimix_state()

def monitor_poll():
    for name in list(monitored_sources.keys()):
        props = monitored_sources.get(name)
        if props is None:
            continue
        if find_index_by_name(name) is None:
            monitor_misses[name] = monitor_misses.get(name, 0) + 1
            if monitor_misses[name] >= MONITOR_MAX_MISSES:
                del monitored_sources[name]
                del monitor_misses[name]
                if LOG_LEVEL >= 1:
                    print(f"{COLOR_TIMESTAMP}[MONITOR]{COLOR_RESET} removed: '{name}' (source no longer present)")
                broadcast_vimix_state()
            continue
        monitor_misses[name] = 0
        request_source_props(name, props)

def process_vimix_current_message(address, args):
    global source_current
    match = re.match(r"^/?vimix/current/(\d+)$", address)
    if match:
        idx = int(match.group(1))
        if idx == 0:
            start_new_sync_round()
        seen_indices.add(idx)
        val = args[0] if args else None
        if val in (1, 1.0, "1"):
            if source_current != idx:
                source_current = idx
                return True, idx, True
            return True, idx, False
    return False, None, False

def process_vimix_message(address, args):
    match = re.match(r"^/?vimix/([^/]+)/(.+)$", address)
    if match:
        target_str, prop_name = match.group(1), match.group(2)
        if prop_name not in SUPPORTED_PROPERTIES: return None

        idx = int(target_str) if target_str.isdigit() else find_index_by_name(target_str)
        if idx is None: return None
        val = args[0] if len(args) == 1 else list(args) if len(args) > 1 else None

        if idx not in vimix_data: vimix_data[idx] = create_empty_vimix_entry()

        if prop_name == "name":
            current_name = vimix_data[idx].get("name")
            if current_name is None:
                if idx not in known_indices and val:
                    known_indices.add(idx)
                    request_uri_once(str(val))
            elif current_name != val:
                if LOG_LEVEL >= 1:
                    print(f"{COLOR_TIMESTAMP}[NAME]{COLOR_RESET} Source {idx}: '{current_name}' -> '{val}'")
                if val:
                    request_uri_once(str(val))

        current_val = vimix_data[idx].get(prop_name)
        is_changed = (current_val != val)
        vimix_data[idx][prop_name] = val

        if is_changed and LOG_LEVEL >= 1:
            print(f"{COLOR_TIMESTAMP}[VIMIX DATA]{COLOR_RESET} Source {idx} | {prop_name}: {val}")

        if prop_name == "uri" and is_changed and val:
            threading.Thread(target=generate_thumbnails_worker, args=(idx, val), daemon=True).start()

        return idx, prop_name, val, is_changed
    return None

def create_osc_handler(server_port):
    def osc_handler(client_address, address, *args):
        clean_address = address.lstrip('/')

        if clean_address.startswith("viosc/thumb/"):
            target_identifier = clean_address.replace("viosc/thumb/", "", 1)
            thumb_arg = args[0] if args else "all"
            send_thumbnail_blob(target_identifier, thumb_arg)
        elif clean_address.startswith("viosc/regen_thumb/"):
            target_identifier = clean_address.replace("viosc/regen_thumb/", "", 1)
            idx = int(target_identifier) if target_identifier.isdigit() else find_index_by_name(target_identifier)
            if idx is not None and idx in vimix_data:
                uri_val = vimix_data[idx].get("uri")
                if uri_val:
                    vimix_data[idx]["thumbnails"] = []
                    threading.Thread(target=generate_thumbnails_worker, args=(idx, uri_val), daemon=True).start()
        elif clean_address.startswith("viosc/sync/"): process_dynamic_sync_request(clean_address, args)
        elif clean_address.startswith("viosc/monitor/"):
            target_name = clean_address.replace("viosc/monitor/", "", 1)
            process_monitor_command(target_name, args)
        elif clean_address.startswith("viosc"): pass
        elif server_port == FROMVIMIX_PORT and clean_address.startswith("vimix/"):
            if clean_address.startswith("vimix/current/"):
                is_valid, idx, is_changed = process_vimix_current_message(address, args)
                if is_valid and is_changed: broadcast_vimix_state()
            else:
                parsed = process_vimix_message(address, args)
                if parsed:
                    idx, prop_name, val, is_changed = parsed
                    if is_changed: broadcast_vimix_state()
        elif server_port == LISTEN_PORT:
            forward_client.send_message(address, list(args))
    return osc_handler

def sync_loop():
    while True:
        try: forward_client.send_message("/vimix/current/sync", [])
        except Exception: pass
        monitor_poll()
        time.sleep(sync_interval_time / 1000.0)

def start_server_instance(ip, port):
    dispatcher = Dispatcher()
    handler = create_osc_handler(port)
    dispatcher.set_default_handler(handler, needs_reply_address=True)
    server = BlockingOSCUDPServer((ip, port), dispatcher)
    server.serve_forever()

def verify_dependencies():
    if shutil.which(FFMPEG_PATH) is None:
        print(f"ERROR: ffmpeg executable not found ('{FFMPEG_PATH}').")
        print("Install ffmpeg, or set the VIOSC_FFMPEG environment variable to the full path of the executable.")
        sys.exit(1)
    if shutil.which(FFPROBE_PATH) is None:
        print(f"ERROR: ffprobe executable not found ('{FFPROBE_PATH}').")
        print("Install ffprobe (part of ffmpeg), or set the VIOSC_FFPROBE environment variable to the full path of the executable.")
        sys.exit(1)

def main():
    verify_dependencies()
    print("==========================================================================================")
    print("                      MULTI-PORT OSC ROUTER 'viOSC' STARTED                           ")
    print(f"   input   : {LISTEN_IP}:{LISTEN_PORT}   (commands -> vimix {TOVIMIX_IP}:{TOVIMIX_PORT})")
    print(f"   from vimix: {LOCAL_BIND_IP}:{FROMVIMIX_PORT}   (vimix default OSC replies)")
    print(f"   output  : {UI_IP}:{REPLY_PORT}   (state / thumbnails / monitor for the UI)")
    print("==========================================================================================")
    threading.Thread(target=sync_loop, daemon=True).start()
    
    threading.Thread(target=start_server_instance, args=(LISTEN_IP, LISTEN_PORT), daemon=True).start()
    threading.Thread(target=start_server_instance, args=(LOCAL_BIND_IP, FROMVIMIX_PORT), daemon=True).start()
        
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: print("\nAll servers stopped.")

if __name__ == "__main__": main()
