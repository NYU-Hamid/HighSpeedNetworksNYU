import threading
import time
import random
import math
import networkx as nx
import json
import logging
import webbrowser
from flask import Flask, Response, request
from flask_socketio import SocketIO, emit

# ==============================================================================
#  SCIENTIFIC CONFIGURATION (REPS PAPER)
# ==============================================================================
REPS_BUFFER_SIZE = 8
PACKET_SIZE_BYTES = 1500
SIM_TICK_SEC = 0.05
EWMA_ALPHA = 0.1
DROP_ANIMATION_FRAMES = 30
NUM_PKTS_BDP = 100
# Paper Section 3.2: "In case of a network failure, REPS re-routes traffic away
# from it in less than 100 microseconds."
# We use a tick count that represents a "few RTTs" to simulate this timeout.
FREEZING_TIMEOUT_TICKS = 100
EVS_SIZE = 65536

DEFAULT_CONFIG = {
    'num_pods': 2, 'num_cores': 2, 'aggs_per_pod': 2,
    'edges_per_pod': 2, 'hosts_per_edge': 2, 'queue_cap': 16
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ==============================================================================
#  BACKEND SIMULATION
# ==============================================================================
class REPSSwitch:
    def __init__(self, id, layer, capacity):
        self.id = id
        self.layer = layer
        self.capacity = capacity
        self.queues = {}
        # Paper Section 2.1: ECN marking threshold (K_min) is 20% of queue capacity
        self.ecn_threshold = max(1.0, capacity * 0.2)

    def enqueue(self, next_hop):
        if next_hop not in self.queues: self.queues[next_hop] = 0
        if self.queues[next_hop] >= self.capacity:
            return False
        self.queues[next_hop] += 1
        return True

    def dequeue(self, next_hop):
        if next_hop in self.queues and self.queues[next_hop] > 0: self.queues[next_hop] -= 1

    def get_load(self, next_hop):
        return self.queues.get(next_hop, 0)

    def check_ecn(self, next_hop):
        return self.get_load(next_hop) > self.ecn_threshold


class REPSHost:
    def __init__(self, id):
        self.id = id
        # Paper Algo 1: repsBuffer
        self.buffer = [{'ev': None, 'valid': False} for _ in range(REPS_BUFFER_SIZE)]
        self.head = 0
        self.num_valid_evs = 0
        self.is_freezing_mode = False
        self.explore_counter = NUM_PKTS_BDP
        # Stats for visualization
        self.reused_count = 0
        self.explored_count = 0
        self.discarded_count = 0
        self.freezing_entered_time = None
        self.freezing_exit_tick = None

    # --------------------------------------------------------------------------
    # PAPER ALGORITHM 2: REPS logic on send datapath
    # --------------------------------------------------------------------------
    def get_next_ev(self):
        # Line 15: if repsBuffer.isEmpty() or (numberOfValidEVs == 0 and not isFreezingMode) or exploreCounter

        # Check if buffer is truly empty (all None) - initial state
        buffer_is_empty = all(entry['ev'] is None for entry in self.buffer)

        # We only explore (random EV) if:
        # 1. We are NOT in freezing mode AND (buffer empty OR no valid EVs)
        # 2. OR we are strictly in the initial exploration phase (exploreCounter > 0)
        should_explore = (buffer_is_empty and not self.is_freezing_mode) or \
                         (self.num_valid_evs == 0 and not self.is_freezing_mode) or \
                         (self.explore_counter > 0)

        if should_explore:
            # Line 16: dataPacket.ev = rand() % EVS_SIZE
            ev = random.randint(0, EVS_SIZE - 1)
            # Line 17: exploreCounter = max(exploreCounter - 1, 0)
            self.explore_counter = max(self.explore_counter - 1, 0)
            self.explored_count += 1
            return ev
        else:
            # Line 19: dataPacket.ev = getNextEV()
            return self._get_next_ev_procedure()

    # PAPER ALGORITHM 2: procedure getNextEV()
    def _get_next_ev_procedure(self):
        if self.num_valid_evs > 0:
            # Line 4: offset = (head - numberOfValidEVs) % REPS_BUFFER_SIZE
            offset = (self.head - self.num_valid_evs) % REPS_BUFFER_SIZE
            ev = self.buffer[offset]['ev']

            # Line 5: repsBuffer[offset].isValid = false
            self.buffer[offset]['valid'] = False

            # Line 6: numberOfValidEVs --
            self.num_valid_evs -= 1

            self.reused_count += 1
            return ev
        else:
            # Line 7: else (This implies we MUST be in freezing mode or buffer had items but marked invalid)
            # In Freezing Mode, we reuse INVALID items to avoid random exploration.

            # Line 8: offset = head
            offset = self.head

            # Line 9: head = (head + 1) % REPS_BUFFER_SIZE
            self.head = (self.head + 1) % REPS_BUFFER_SIZE

            ev = self.buffer[offset]['ev']

            # Edge Case: If we entered freezing mode but buffer was totally empty (start of sim),
            # we must fallback to random, otherwise we send None.
            if ev is None:
                ev = random.randint(0, EVS_SIZE - 1)

            self.reused_count += 1
            return ev

    # --------------------------------------------------------------------------
    # PAPER ALGORITHM 1: procedure onAck(ackPacket)
    # --------------------------------------------------------------------------
    def on_ack(self, ev, ecn, current_tick):
        # Line 6: if ackPacket.ecn is set then return
        if ecn:
            self.discarded_count += 1
            return

        # Line 9: if not repsBuffer[head].isValid then numberOfValidEVs ++
        if not self.buffer[self.head]['valid']:
            self.num_valid_evs = min(self.num_valid_evs + 1, REPS_BUFFER_SIZE)

        # Line 12: repsBuffer[head].cachedEV = ackPacket.ev
        self.buffer[self.head]['ev'] = ev
        # Line 13: repsBuffer[head].isValid = true
        self.buffer[self.head]['valid'] = True
        # Line 14: head = (head + 1) % REPS_BUFFER_SIZE
        self.head = (self.head + 1) % REPS_BUFFER_SIZE

        # Line 15: if isFreezingMode and now() > exitFreezingMode
        # (We check this on every ACK to see if we can relax)
        if self.is_freezing_mode and self.freezing_exit_tick and current_tick >= self.freezing_exit_tick:
            # Line 16: isFreezingMode = false
            self.is_freezing_mode = False
            # Line 17: exploreCounter = NUM_PKTS_BDP
            # "Once we exit freezing mode, we use random EVs to allow REPS to explore new paths"
            self.explore_counter = NUM_PKTS_BDP

    # --------------------------------------------------------------------------
    # PAPER ALGORITHM 1: procedure onFailureDetection()
    # --------------------------------------------------------------------------
    # This is called when we detect a drop (timeout or explicit NACK/drop signal)
    def on_failure_detection(self, current_tick):
        # Line 22: if not isFreezingMode and exploreCounter == 0
        if not self.is_freezing_mode and self.explore_counter == 0:
            # Line 23: isFreezingMode = true
            self.is_freezing_mode = True
            # Line 24: exitFreezingMode = now() + FREEZING_TIMEOUT
            self.freezing_entered_time = current_tick * SIM_TICK_SEC
            self.freezing_exit_tick = current_tick + FREEZING_TIMEOUT_TICKS

    # Helper to check timeout even if no ACKs arrive (since we might be fully blocked)
    def check_timeout(self, current_tick):
        if self.is_freezing_mode and self.freezing_exit_tick and current_tick >= self.freezing_exit_tick:
            self.is_freezing_mode = False
            self.explore_counter = NUM_PKTS_BDP

    def get_entropy_info(self):
        buffer_entries = []
        for i in range(REPS_BUFFER_SIZE):
            entry = self.buffer[i]
            buffer_entries.append({
                'index': i, 'ev': entry['ev'], 'valid': entry['valid'], 'is_head': (i == self.head)
            })
        return {
            'id': self.id, 'buffer_entries': buffer_entries, 'num_valid': self.num_valid_evs,
            'head_position': self.head, 'reused_count': self.reused_count,
            'explored_count': self.explored_count, 'discarded_count': self.discarded_count,
            'explore_counter': self.explore_counter, 'is_freezing_mode': self.is_freezing_mode,
            'freezing_entered_time': self.freezing_entered_time
        }


class NetworkSimulation:
    def __init__(self):
        self.lock = threading.Lock()
        self.dropped_packets_count = 0
        self.reset_topology()
        self.speed_multiplier = 1.0
        self.current_tick = 0

    def reset_topology(self, config=None):
        with self.lock:
            self.graph = nx.Graph()
            self.nodes = {}
            self.links = []
            self.packets = []
            self.pkt_ctr = 0
            self.running = False
            self.dropped_packets_count = 0
            self.config = config if config else DEFAULT_CONFIG
            self.current_tick = 0
            self._build_topology()

    def _build_topology(self):
        C, P, A, E_pod, H_edge = int(self.config['num_cores']), int(self.config['num_pods']), int(
            self.config['aggs_per_pod']), int(self.config['edges_per_pod']), int(self.config['hosts_per_edge'])
        Q = int(self.config['queue_cap'])

        core_ids = [f"C{i + 1}" for i in range(C)]
        for i, c in enumerate(core_ids):
            self._add_node(c, 'core', (i + 0.5) * (100 / C), 10, capacity=Q)

        pod_width = 100 / P
        for p in range(P):
            px = p * pod_width
            aggs = []
            for a in range(A):
                aid = f"A{p + 1}_{a + 1}"
                self._add_node(aid, 'agg', px + (a + 0.5) * (pod_width / A), 30, capacity=Q)
                aggs.append(aid)
                for c in core_ids: self._add_link(aid, c)

            for e in range(E_pod):
                eid = f"E{p + 1}_{e + 1}"
                self._add_node(eid, 'edge', px + (e + 0.5) * (pod_width / E_pod), 60, capacity=Q)
                for agg in aggs: self._add_link(eid, agg)
                h_spread = (pod_width / E_pod) * 0.8
                h_start = (px + (e + 0.5) * (pod_width / E_pod)) - (h_spread / 2)
                for h in range(H_edge):
                    hid = f"H{p + 1}_{e + 1}_{h + 1}"
                    self._add_node(hid, 'host', h_start + (h + 0.5) * (h_spread / H_edge), 90, 0)
                    self._add_link(eid, hid)

    def _add_node(self, id, type, x, y, capacity=4):
        self.graph.add_node(id, type=type, x=x, y=y)
        if type == 'host':
            self.nodes[id] = REPSHost(id)
        else:
            self.nodes[id] = REPSSwitch(id, type, capacity)

    def _add_link(self, u, v):
        key = tuple(sorted((u, v)))
        if any(l['key'] == key for l in self.links): return
        self.graph.add_edge(u, v)
        self.links.append({'key': key, 'u': u, 'v': v, 'active': 0, 'bw': 10, 'throughput': 0.0, 'bytes_this_tick': 0})

    def update_link_bw(self, u, v, bw):
        key = tuple(sorted((u, v)))
        with self.lock:
            for l in self.links:
                if l['key'] == key:
                    l['bw'] = int(bw)
                    break

    def step(self):
        with self.lock:
            self.current_tick += 1

            # 1. Flow Generation
            src_id = "H1_1_1"
            dst_id = "H2_2_2"
            if self.nodes.get(src_id) and self.nodes.get(dst_id):
                if random.random() < 0.40:
                    src_node = self.nodes[src_id]
                    ev = src_node.get_next_ev()
                    try:
                        all_paths = list(nx.all_shortest_paths(self.graph, src_id, dst_id))
                        path = all_paths[ev % len(all_paths)]
                        self.pkt_ctr += 1
                        self.packets.append({
                            'id': self.pkt_ctr, 'type': 'DATA', 'src': src_id, 'dst': dst_id,
                            'ev': ev, 'ecn': False, 'ack': False, 'path': path, 'hop': 0, 'pct': 0.0,
                            'drop': False, 'drop_timer': 0, 'acked_id': None
                        })
                    except:
                        pass

            for node in self.nodes.values():
                if isinstance(node, REPSHost): node.check_timeout(self.current_tick)

            # 2. Update Links (EWMA)
            for l in self.links:
                bits_sec = (l['bytes_this_tick'] * 8) / SIM_TICK_SEC
                l['throughput'] = (1.0 - EWMA_ALPHA) * l['throughput'] + (EWMA_ALPHA * bits_sec)
                l['bytes_this_tick'] = 0
                if l['active'] > 0: l['active'] -= 1

            # 3. Move Packets
            alive = []
            base_speed = 0.03 * self.speed_multiplier

            for p in self.packets:
                if p['drop']:
                    p['drop_timer'] -= 1
                    if p['drop_timer'] > 0: alive.append(p)
                    continue

                # SCALED SPEED LOGIC
                current_speed = base_speed
                if p['hop'] < len(p['path']) - 1:
                    u, v = p['path'][p['hop']], p['path'][p['hop'] + 1]
                    key = tuple(sorted((u, v)))
                    for l in self.links:
                        if l['key'] == key:
                            # If bandwidth is 0, speed is 0 (packet stops/fails)
                            current_speed = base_speed * (l['bw'] / 10.0)
                            break

                p['pct'] += current_speed

                # Link Utilization
                if p['hop'] < len(p['path']) - 1:
                    u, v = p['path'][p['hop']], p['path'][p['hop'] + 1]
                    key = tuple(sorted((u, v)))
                    if p['pct'] <= current_speed and current_speed > 0.0001:
                        for l in self.links:
                            if l['key'] == key:
                                l['bytes_this_tick'] += PACKET_SIZE_BYTES;
                                l['active'] = 5;
                                break

                # Arrival Logic
                if p['pct'] >= 1.0:
                    curr_node_id = p['path'][p['hop']]

                    if p['hop'] == len(p['path']) - 1:
                        if p['type'] == 'DATA':
                            ack = p.copy()
                            ack.update({'type': 'ACK', 'ack': True, 'src': p['dst'], 'dst': p['src'],
                                        'path': p['path'][::-1], 'hop': 0, 'pct': 0.0, 'id': self.pkt_ctr + 90000,
                                        'acked_id': p['id'], 'drop': False, 'drop_timer': 0})
                            alive.append(ack)
                        elif p['type'] == 'ACK':
                            if p['dst'] in self.nodes:
                                self.nodes[p['dst']].on_ack(p['ev'], p['ecn'], self.current_tick)
                        continue

                    next_id = p['path'][p['hop'] + 1]
                    curr_obj = self.nodes.get(curr_node_id)
                    next_obj = self.nodes.get(next_id)

                    if isinstance(curr_obj, REPSSwitch): curr_obj.dequeue(next_id)

                    if isinstance(next_obj, REPSSwitch):
                        if p['hop'] + 2 < len(p['path']):
                            outgoing = p['path'][p['hop'] + 2]
                            if not next_obj.enqueue(outgoing):
                                self.dropped_packets_count += 1
                                p['drop'] = True
                                p['drop_timer'] = DROP_ANIMATION_FRAMES
                                p['pct'] = 1.0
                                if p['src'] in self.nodes:
                                    sender = self.nodes[p['src']]
                                    # REPS paper section 3.2: Detect failure -> Freezing Mode
                                    if isinstance(sender, REPSHost):
                                        sender.on_failure_detection(self.current_tick)
                                alive.append(p)
                                continue
                            if next_obj.check_ecn(outgoing): p['ecn'] = True

                    p['hop'] += 1
                    p['pct'] = 0.0
                    alive.append(p)
                else:
                    alive.append(p)
            self.packets = alive

    def get_state(self):
        with self.lock:
            nd = {}
            for nid, node in self.nodes.items():
                m = self.graph.nodes[nid]
                d = {'x': m['x'], 'y': m['y'], 'type': m['type']}
                if isinstance(node, REPSSwitch):
                    d['q'] = max(node.queues.values()) if node.queues else 0
                    d['cap'] = node.capacity
                elif isinstance(node, REPSHost):
                    d['entropy_info'] = node.get_entropy_info()
                nd[nid] = d

            pd = []
            for p in self.packets:
                try:
                    u = p['path'][p['hop']]
                    v = p['path'][min(p['hop'] + 1, len(p['path']) - 1)]
                    pd.append({'id': p['id'], 'u': u, 'v': v, 'pct': p['pct'], 'ev': p['ev'],
                               'ack': p['ack'], 'ecn': p['ecn'], 'acked_id': p.get('acked_id'),
                               'drop': p.get('drop', False), 'drop_timer': p.get('drop_timer', 0)})
                except:
                    pass

            ld = []
            for l in self.links:
                cap_bps = l['bw'] * 200000.0
                util = (l['throughput'] / cap_bps) * 100.0 if cap_bps > 0 else 0.0
                ld.append({'u': l['u'], 'v': l['v'], 'bw': l['bw'], 'util': min(100.0, util), 'active': l['active']})

            return {'nodes': nd, 'links': ld, 'packets': pd, 'dropped_packets': self.dropped_packets_count}


# ==============================================================================
#  FRONTEND
# ==============================================================================
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
sim = NetworkSimulation()

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>REPS Simulator</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { margin:0; background:#f4f4f9; font-family:'Segoe UI',sans-serif; overflow:hidden; }
        canvas { display:block; }
        #config-panel { position:absolute; top:20px; left:20px; background:#fff; padding:15px; border-radius:8px; box-shadow:0 4px 15px rgba(0,0,0,0.1); width:200px; border-left: 5px solid #2980b9; }
        .inp-group { margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; }
        label { font-size:12px; color:#555; }
        input { width:40px; padding:3px; border:1px solid #ddd; border-radius:4px; text-align:center; }
        button.action { width:100%; background:#2980b9; color:#fff; border:none; padding:8px; border-radius:4px; cursor:pointer; font-weight:bold; margin-top:5px; }
        #link-modal { display:none; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:#fff; padding:20px; border-radius:8px; box-shadow:0 10px 30px rgba(0,0,0,0.3); z-index:100; border:1px solid #ccc; width:220px; }
        #entropy-modal { display:none; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:#fff; padding:20px; border-radius:8px; box-shadow:0 10px 30px rgba(0,0,0,0.3); z-index:101; border:1px solid #ccc; width:480px; max-height:85vh; overflow-y:auto; }
        #modal-overlay { display:none; position:absolute; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.2); z-index:90; }
        #controls { position:absolute; bottom:20px; right:20px; z-index:10; display:flex; gap:10px; align-items:center; }
        .ctrl-btn { background:#333; color:#fff; border:none; padding:10px 20px; border-radius:4px; font-weight:bold; cursor:pointer; }
        .slider-container { background:rgba(255,255,255,0.9); padding:10px; border-radius:4px; display:flex; align-items:center; gap:10px; }
        input[type=range] { width: 100px; }
        .modal-close { position:absolute; top:10px; right:15px; cursor:pointer; font-size:20px; color:#999; }
        .entropy-section { margin-bottom:15px; border-bottom:1px solid #eee; padding-bottom:12px; }
        .entropy-stat { display:flex; justify-content:space-between; padding:4px 0; font-size:11px; margin:3px 0; }
        .entropy-stat-label { color:#555; font-weight:bold; }
        .entropy-stat-value { color:#2980b9; font-weight:bold; font-family:monospace; }
        .buffer-entry { padding:8px; margin:4px 0; background:#f9f9f9; border-left:4px solid #ddd; border-radius:3px; font-family:monospace; font-size:11px; color:#333; }
        .buffer-entry.valid { background:#e8f5e9; border-left-color:#4caf50; color:#2e7d32; font-weight:bold; }
        .buffer-entry.head { background:#fff3e0; border-left-color:#ff9800; color:#e65100; font-weight:bold; }
        #entropy-title { color:#2980b9; margin:0 0 12px 0; font-size:16px; border-bottom:2px solid #2980b9; padding-bottom:8px; }
        .section-title { font-size:11px; font-weight:bold; color:#333; margin:8px 0 6px 0; padding:4px; background:#f0f0f0; border-radius:3px; }
        .freezing-indicator { padding:8px; margin:10px 0; border-radius:4px; font-weight:bold; font-size:11px; text-align:center; background:#f5f5f5; color:#666; border:1px solid #ddd; }
        .freezing-indicator.active { background:#ffebee; color:#c62828; border:1px solid #c62828; }
    </style>
</head>
<body>
    <div id="config-panel">
        <h3>Topology</h3>
        <div class="inp-group"><label>Cores</label><input id="inp-c" type="number" value="2" min="1"></div>
        <div class="inp-group"><label>Pods</label><input id="inp-p" type="number" value="2" min="1"></div>
        <div class="inp-group"><label>Aggs</label><input id="inp-a" type="number" value="2" min="1"></div>
        <div class="inp-group"><label>Edges</label><input id="inp-e" type="number" value="2" min="1"></div>
        <div class="inp-group"><label>Hosts</label><input id="inp-h" type="number" value="2" min="1"></div>
        <div class="inp-group"><label>Q Size</label><input id="inp-q" type="number" value="16" min="4"></div>

        <div class="inp-group" style="margin-top:10px; padding-top:5px; border-top:1px solid #eee;">
            <label style="color:#c0392b; font-weight:bold;">Dropped:</label>
            <span id="disp-drops" style="font-weight:bold; color:#c0392b;">0</span>
        </div>

        <button class="action" onclick="apply()">REBUILD</button>
    </div>

    <div id="controls">
        <div class="slider-container">
            <label style="font-weight:bold;">Speed:</label>
            <input type="range" min="0.1" max="5.0" step="0.1" value="1.0" oninput="emit('speed', this.value)">
        </div>
        <button class="ctrl-btn" onclick="emit('start')">START</button>
        <button class="ctrl-btn" onclick="emit('pause')">PAUSE</button>
    </div>

    <div id="modal-overlay" onclick="closeModals()"></div>

    <div id="link-modal">
        <span class="modal-close" onclick="closeModals()">&times;</span>
        <h4>Link Config</h4>
        <div class="inp-group"><label>BW (Gbps):</label><input id="link-bw" type="number" value="10"></div>
        <input type="hidden" id="lu"><input type="hidden" id="lv">
        <div style="text-align:right; margin-top:15px;"><button onclick="saveLink()" style="background:#2980b9; color:#fff; border:none; padding:5px 10px; border-radius:4px;">Save</button></div>
    </div>

    <div id="entropy-modal">
        <span class="modal-close" onclick="closeModals()">&times;</span>
        <h2 id="entropy-title">Entropy Buffer</h2>

        <div id="freezing-indicator" class="freezing-indicator">Freezing Mode: OFF (Section 3.2)</div>

        <div class="entropy-section">
            <div class="section-title">Algorithm 1: Path Caching</div>
            <div class="entropy-stat">
                <span class="entropy-stat-label">Valid Entries:</span>
                <span class="entropy-stat-value" id="ent-valid">0/8</span>
            </div>
            <div class="entropy-stat">
                <span class="entropy-stat-label">Discarded (ECN):</span>
                <span class="entropy-stat-value" id="ent-discarded">0</span>
            </div>
        </div>

        <div class="entropy-section">
            <div class="section-title">Algorithm 2: Path Selection </div>
            <div class="entropy-stat">
                <span class="entropy-stat-label">Reused Count:</span>
                <span class="entropy-stat-value" id="ent-reused">0</span>
            </div>
            <div class="entropy-stat">
                <span class="entropy-stat-label">Explored Count:</span>
                <span class="entropy-stat-value" id="ent-explored">0</span>
            </div>
            <div class="entropy-stat">
                <span class="entropy-stat-label">Explore Counter:</span>
                <span class="entropy-stat-value" id="ent-explore-counter">100</span>
            </div>
        </div>

        <div class="entropy-section">
            <div class="section-title">Circular Buffer State </div>
            <div class="entropy-stat">
                <span class="entropy-stat-label">Head Position:</span>
                <span class="entropy-stat-value" id="ent-head">0</span>
            </div>
            <div class="entropy-stat">
                <span class="entropy-stat-label">Freezing Status:</span>
                <span class="entropy-stat-value" id="ent-freeze-status">OFF</span>
            </div>
            <div class="entropy-stat">
                <span class="entropy-stat-label">Entered At (seconds):</span>
                <span class="entropy-stat-value" id="ent-freeze-time">Never</span>
            </div>
        </div>

        <div class="entropy-section">
            <div class="section-title">Buffer Contents (8 Entries)</div>
            <div id="ent-buffer-list"></div>
        </div>
    </div>

    <canvas id="c"></canvas>
    <script>
        const socket = io();
        const cvs = document.getElementById('c');
        const ctx = cvs.getContext('2d');
        let W, H, state = null;
        let linkHitboxes = [];
        let nodeHitboxes = [];
        const NODE_WIDTH = 80, NODE_HEIGHT = 40;
        const PORT_RADIUS = 3.5; 

        function resize() { W=window.innerWidth; H=window.innerHeight; cvs.width=W; cvs.height=H; }
        window.addEventListener('resize', resize); resize();

        socket.on('connect', () => { console.log("Connected"); });
        socket.on('frame', d => { state = d; });
        function emit(cmd, val) { socket.emit('ctrl', {cmd, val}); }
        function apply() { emit('reset', { num_cores: document.getElementById('inp-c').value, num_pods: document.getElementById('inp-p').value, aggs_per_pod: document.getElementById('inp-a').value, edges_per_pod: document.getElementById('inp-e').value, hosts_per_edge: document.getElementById('inp-h').value, queue_cap: document.getElementById('inp-q').value }); }

        function closeModals() {
            document.getElementById('modal-overlay').style.display='none'; 
            document.getElementById('link-modal').style.display='none';
            document.getElementById('entropy-modal').style.display='none';
        }

        function openEntropyModal(nodeId, entropyInfo) {
            document.getElementById('entropy-title').innerText = 'Entropy Buffer: ' + nodeId;
            document.getElementById('ent-valid').innerText = entropyInfo.num_valid + '/8';
            document.getElementById('ent-discarded').innerText = entropyInfo.discarded_count;
            document.getElementById('ent-reused').innerText = entropyInfo.reused_count;
            document.getElementById('ent-explored').innerText = entropyInfo.explored_count;
            document.getElementById('ent-explore-counter').innerText = entropyInfo.explore_counter;
            document.getElementById('ent-head').innerText = entropyInfo.head_position;

            const freezingIndicator = document.getElementById('freezing-indicator');
            if(entropyInfo.is_freezing_mode) {
                freezingIndicator.className = 'freezing-indicator active';
                freezingIndicator.innerHTML = 'Freezing Mode: ON';
                document.getElementById('ent-freeze-status').innerText = 'ON';
                const freezeTime = entropyInfo.freezing_entered_time ? entropyInfo.freezing_entered_time.toFixed(3) + 's' : 'Now';
                document.getElementById('ent-freeze-time').innerText = freezeTime;
            } else {
                freezingIndicator.className = 'freezing-indicator';
                freezingIndicator.innerHTML = 'Freezing Mode: OFF ';
                document.getElementById('ent-freeze-status').innerText = 'OFF';
                document.getElementById('ent-freeze-time').innerText = entropyInfo.freezing_entered_time ? entropyInfo.freezing_entered_time.toFixed(3) + 's (exited)' : 'Never';
            }

            let bufferHtml = '';
            for(let entry of entropyInfo.buffer_entries) {
                let classes = 'buffer-entry';
                let evStr = '';
                let status = '';

                if(entry.ev !== null && entry.ev !== undefined) {
                    evStr = entry.ev.toString();
                } else {
                    evStr = '—';
                }

                if(entry.is_head) {
                    classes += ' head';
                    status = ' (HEAD)';
                } else if(entry.valid) {
                    classes += ' valid';
                }

                bufferHtml += `<div class="${classes}">[${entry.index}] EV = ${evStr}${status}</div>`;
            }
            document.getElementById('ent-buffer-list').innerHTML = bufferHtml;

            document.getElementById('modal-overlay').style.display='block';
            document.getElementById('entropy-modal').style.display='block';
        }

        cvs.addEventListener('click', e => {
            const r = cvs.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top;

            for(let nh of nodeHitboxes) {
                const dx = mx - nh.cx, dy = my - nh.cy;
                if(Math.sqrt(dx*dx + dy*dy) < 30 && nh.type === 'host') {
                    openEntropyModal(nh.id, nh.entropy_info);
                    return;
                }
            }

            for(let b of linkHitboxes) { 
                if(pointToLineDist(mx, my, b.x1, b.y1, b.x2, b.y2) < 10) { 
                    openModal(b.u, b.v, b.bw); 
                    return; 
                } 
            }
        });

        function pointToLineDist(x, y, x1, y1, x2, y2) { const A=x-x1, B=y-y1, C=x2-x1, D=y2-y1; const dot=A*C+B*D, len_sq=C*C+D*D; let param=-1; if(len_sq!=0) param=dot/len_sq; let xx, yy; if(param<0){xx=x1;yy=y1}else if(param>1){xx=x2;yy=y2}else{xx=x1+param*C;yy=y1+param*D} return Math.sqrt(Math.pow(x-xx,2)+Math.pow(y-yy,2)); }
        function openModal(u, v, bw) { document.getElementById('lu').value=u; document.getElementById('lv').value=v; document.getElementById('link-bw').value=bw; document.getElementById('modal-overlay').style.display='block'; document.getElementById('link-modal').style.display='block'; }
        function saveLink() { emit('set_link_bw', { u: document.getElementById('lu').value, v: document.getElementById('lv').value, bw: document.getElementById('link-bw').value }); closeModals(); }

        function getPortPosition(cx, cy, w, h, tx, ty) {
            const dx = tx - cx; const dy = ty - cy;
            if(dx === 0 && dy === 0) return {x:cx, y:cy}; 
            let m = dy / dx; let x, y;
            if (Math.abs(dx) * h > Math.abs(dy) * w) { x = dx > 0 ? cx + w/2 : cx - w/2; y = cy + m * (x - cx); } 
            else { y = dy > 0 ? cy + h/2 : cy - h/2; x = cx + (dx !== 0 ? (y - cy) / m : 0); }
            return {x, y};
        }

        function getLowerNode(n1, n2) {
            const types = {'host':0, 'edge':1, 'agg':2, 'core':3};
            const t1 = types[n1.type], t2 = types[n2.type];
            return (t1 < t2) ? n1 : n2;
        }

        function draw() {
            ctx.clearRect(0,0,W,H);
            if(!state) { requestAnimationFrame(draw); return; }

            if(document.getElementById('disp-drops')) {
                document.getElementById('disp-drops').innerText = state.dropped_packets || 0;
            }

            linkHitboxes = [];
            nodeHitboxes = [];

            state.links.forEach(l => {
                const n1 = state.nodes[l.u], n2 = state.nodes[l.v];
                const p1 = {x:n1.x/100*W, y:n1.y/100*H}, p2 = {x:n2.x/100*W, y:n2.y/100*H};

                ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);

                // VISUAL FIX: If bandwidth is 0, use a very light/faded color
                if (l.bw === 0) {
                    ctx.strokeStyle = '#ecf0f1'; // Very light gray (almost invisible)
                    ctx.lineWidth = 1; // Thinner
                } else {
                    ctx.strokeStyle = l.active > 0 ? '#3498db' : '#bdc3c7'; 
                    ctx.lineWidth = l.active > 0 ? 3 : 2; 
                }

                ctx.stroke();
                linkHitboxes.push({u:l.u, v:l.v, x1:p1.x, y1:p1.y, x2:p2.x, y2:p2.y, bw:l.bw});

                const lowerNode = getLowerNode(n1, n2);
                const higherNode = (lowerNode.id === n1.id) ? n2 : n1;
                const lowerPoint = (lowerNode.id === n1.id) ? p1 : p2;
                const higherPoint = (lowerNode.id === n1.id) ? p2 : p1;

                let targetX, targetY;
                if (lowerNode.type === 'host') {
                    const t = 0.80;
                    targetX = lowerPoint.x + (higherPoint.x - lowerPoint.x) * t;
                    targetY = lowerPoint.y + (higherPoint.y - lowerPoint.y) * t;
                } else {
                    const FIXED_RISE = 45;
                    targetY = lowerPoint.y - FIXED_RISE;
                    let t_val = (targetY - p1.y) / (p2.y - p1.y);
                    if(!isFinite(t_val)) t_val = 0.5;
                    if(t_val < 0) t_val = 0; if(t_val > 1) t_val = 1;
                    targetX = p1.x + (p2.x - p1.x) * t_val;
                }

                drawLabelAt(targetX, targetY, l.util, l.bw);
            });

            Object.entries(state.nodes).forEach(([id, n]) => {
                const cx = n.x/100*W, cy = n.y/100*H;
                if(n.type !== 'host') {
                    const w=NODE_WIDTH, h=NODE_HEIGHT;
                    ctx.fillStyle = (n.type==='core') ? '#2980b9' : '#3498db'; ctx.strokeStyle = '#1a5276'; ctx.lineWidth=2;
                    ctx.beginPath(); ctx.rect(cx-w/2, cy-h/2, w, h); ctx.fill(); ctx.stroke();
                    ctx.fillStyle='#fff'; ctx.font="bold 12px Segoe UI"; ctx.textAlign="center"; ctx.fillText(id, cx-12, cy+5);
                    const qx = cx+w/2 - 12, qH = 30;
                    ctx.fillStyle="#ecf0f1"; ctx.fillRect(qx, cy-15, 8, qH); ctx.strokeRect(qx, cy-15, 8, qH);
                    if(n.q > 0) { const hFill = Math.min(1.0, n.q/n.cap)*qH; ctx.fillStyle = n.q >= (n.cap/2) ? '#e74c3c' : '#2ecc71'; ctx.fillRect(qx, cy+15-hFill, 8, hFill); }
                    const pct = Math.round((n.q/n.cap)*100); ctx.fillStyle="#333"; ctx.font="9px Arial"; ctx.textAlign="left"; ctx.fillText(pct+"%", qx+12, cy+5);
                } else {
                    // Host visualization
                    ctx.fillStyle='#34495e'; ctx.fillRect(cx-12, cy-8, 24, 16);
                    ctx.fillStyle='#fff'; ctx.font="9px Segoe UI"; ctx.textAlign="center"; ctx.fillText(id.split('_')[2], cx, cy+4);

                    // Visualize Freezing Mode on the Host itself
                    if (n.entropy_info.is_freezing_mode) {
                        ctx.strokeStyle = '#e74c3c'; ctx.lineWidth = 3;
                        ctx.strokeRect(cx-14, cy-10, 28, 20); // Red border
                        ctx.fillStyle = '#e74c3c'; ctx.font = "bold 9px Arial";
                        ctx.fillText("FREEZE", cx, cy-15);
                    }

                    nodeHitboxes.push({
                        id: id,
                        cx: cx,
                        cy: cy,
                        type: 'host',
                        entropy_info: n.entropy_info
                    });
                }
            });

            state.links.forEach(l => {
                const n1 = state.nodes[l.u], n2 = state.nodes[l.v];
                const p1 = {x:n1.x/100*W, y:n1.y/100*H}, p2 = {x:n2.x/100*W, y:n2.y/100*H};
                if(n1.type !== 'host') {
                    const portPos = getPortPosition(p1.x, p1.y, NODE_WIDTH, NODE_HEIGHT, p2.x, p2.y);
                    ctx.beginPath(); ctx.arc(portPos.x, portPos.y, PORT_RADIUS, 0, 6.28); 
                    ctx.fillStyle='#ecf0f1'; ctx.fill(); ctx.stroke();
                }
                if(n2.type !== 'host') {
                    const portPos = getPortPosition(p2.x, p2.y, NODE_WIDTH, NODE_HEIGHT, p1.x, p1.y);
                    ctx.beginPath(); ctx.arc(portPos.x, portPos.y, PORT_RADIUS, 0, 6.28); 
                    ctx.fillStyle='#ecf0f1'; ctx.fill(); ctx.stroke();
                }
            });

            state.packets.forEach(p => {
                const n1=state.nodes[p.u], n2=state.nodes[p.v];
                const p1={x:n1.x/100*W, y:n1.y/100*H}, p2={x:n2.x/100*W, y:n2.y/100*H};
                const x = p1.x + (p2.x-p1.x)*p.pct; const y = p1.y + (p2.y-p1.y)*p.pct;

                ctx.save();

                if (p.drop) {
                    const MAX_DROP_FRAMES = 30;
                    const progress = (MAX_DROP_FRAMES - p.drop_timer) / MAX_DROP_FRAMES; 
                    const dropDist = 60; 
                    ctx.translate(x, y + (progress * dropDist));
                    ctx.globalAlpha = Math.max(0, 1.0 - progress);
                    ctx.rotate(progress * 0.5); 
                    ctx.fillStyle = '#c0392b'; 
                } 
                else {
                    ctx.translate(x, y);
                    let bgColor = '#2980b9';
                    if (p.ack) bgColor = '#27ae60';
                    else if (p.ecn) bgColor = '#c0392b';
                    ctx.fillStyle = bgColor;
                }

                const w = p.ack ? 26 : 30; 
                const h = p.ack ? 36 : 42;

                ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.0;
                ctx.beginPath(); ctx.roundRect(-w/2, -h/2, w, h, 3);
                ctx.fill(); ctx.stroke();

                ctx.fillStyle = '#fff'; 
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";
                ctx.font = "bold 8px Consolas";

                if (p.drop) {
                    ctx.font = "bold 9px Consolas";
                    ctx.fillText("DROP", 0, 0);
                } else {
                    const idStr = p.ack ? `A:${p.acked_id%100}` : `P:${p.id%100}`;
                    ctx.fillText(idStr, 0, -10);
                    ctx.fillText(`E:${p.ev}`, 0, 0);
                    ctx.fillText(`C:${p.ecn?1:0}`, 0, 10);
                }

                ctx.restore();
            });
            requestAnimationFrame(draw);
        }

        function drawLabelAt(lx, ly, util, bw) {
            const x = lx + 22; 
            const y = ly;      
            const borderColor = '#95a5a6';

            // VISUAL FIX: If bandwidth is 0, fade the label too
            if (bw === 0) {
                ctx.globalAlpha = 0.5;
            }

            ctx.fillStyle = 'rgba(255,255,255,0.95)'; 
            ctx.fillRect(x-22, y-10, 44, 20); 
            ctx.strokeStyle = borderColor; ctx.lineWidth=1; 
            ctx.strokeRect(x-22, y-10, 44, 20);

            ctx.fillStyle = '#000'; ctx.font="bold 10px Segoe UI"; ctx.textAlign="center";
            ctx.fillText(`${util.toFixed(2)}%`, x, y); 
            ctx.fillText(`${bw}Gbps`, x, y + 9);

            ctx.globalAlpha = 1.0; // Reset
        }
        requestAnimationFrame(draw);
    </script>
</body>
</html>
"""


@app.route('/')
def index(): return Response(HTML, mimetype='text/html')


@socketio.on('ctrl')
def handle_ctrl(d):
    if d['cmd'] == 'start':
        sim.running = True
    elif d['cmd'] == 'pause':
        sim.running = False
    elif d['cmd'] == 'reset':
        sim.reset_topology(d['val'])
    elif d['cmd'] == 'set_link_bw':
        sim.update_link_bw(d['val']['u'], d['val']['v'], d['val']['bw'])
    elif d['cmd'] == 'speed':
        sim.speed_multiplier = float(d['val'])


@socketio.on('connect')
def handle_connect(): emit('frame', sim.get_state())


def loop():
    while True:
        if sim.running: sim.step()
        socketio.emit('frame', sim.get_state())
        time.sleep(SIM_TICK_SEC)


if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    webbrowser.open("http://127.0.0.1:5000")
    socketio.run(app, port=5000, allow_unsafe_werkzeug=True)
