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
# Buffer Size 8 (Theorem 5.1)
REPS_BUFFER_SIZE = 8
PACKET_SIZE_BYTES = 1500
SIM_TICK_SEC = 0.05
EWMA_ALPHA = 0.1

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

    def enqueue(self, next_hop):
        if next_hop not in self.queues: self.queues[next_hop] = 0
        if self.queues[next_hop] >= self.capacity: return False
        self.queues[next_hop] += 1
        return True

    def dequeue(self, next_hop):
        if next_hop in self.queues and self.queues[next_hop] > 0: self.queues[next_hop] -= 1

    def get_load(self, next_hop):
        return self.queues.get(next_hop, 0)

    def check_ecn(self, next_hop):
        return self.get_load(next_hop) >= (self.capacity * 0.5)


class REPSHost:
    def __init__(self, id):
        self.id = id
        self.buffer = [{'ev': -1, 'valid': False} for _ in range(REPS_BUFFER_SIZE)]
        self.head = 0

    def get_next_ev(self):
        # Algorithm 2: Reuse Oldest Valid or Explore
        candidate_idx = -1
        for i in range(REPS_BUFFER_SIZE):
            idx = (self.head + i) % REPS_BUFFER_SIZE
            if self.buffer[idx]['valid']:
                candidate_idx = idx
                break

        if candidate_idx != -1:
            ev = self.buffer[candidate_idx]['ev']
            self.buffer[candidate_idx]['valid'] = False
            return ev
        else:
            return random.randint(10, 99)

    def on_ack(self, ev, ecn):
        # Algorithm 1: Cache if no Congestion
        if ecn: return
        self.buffer[self.head] = {'ev': ev, 'valid': True}
        self.head = (self.head + 1) % REPS_BUFFER_SIZE


class NetworkSimulation:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset_topology()
        self.speed_multiplier = 1.0

    def reset_topology(self, config=None):
        with self.lock:
            self.graph = nx.Graph()
            self.nodes = {}
            self.links = []
            self.packets = []
            self.pkt_ctr = 0
            self.running = False
            self.config = config if config else DEFAULT_CONFIG
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
                if l['key'] == key: l['bw'] = int(bw); break

    def step(self):
        with self.lock:
            # 1. Single Flow Generation (Pod 1 -> Pod 2)
            src_id = "H1_1_1"
            dst_id = "H2_2_2"

            if self.nodes.get(src_id) and self.nodes.get(dst_id):
                if random.random() < 0.3:  # Traffic Rate
                    src_node = self.nodes[src_id]
                    ev = src_node.get_next_ev()
                    try:
                        all_paths = list(nx.all_shortest_paths(self.graph, src_id, dst_id))
                        path = all_paths[ev % len(all_paths)]
                        self.pkt_ctr += 1
                        self.packets.append({
                            'id': self.pkt_ctr, 'type': 'DATA',
                            'src': src_id, 'dst': dst_id,
                            'ev': ev, 'ecn': False, 'ack': False,
                            'path': path, 'hop': 0, 'pct': 0.0, 'drop': False, 'acked_id': None
                        })
                    except:
                        pass

            # 2. Update Links
            for l in self.links:
                bits_sec = (l['bytes_this_tick'] * 8) / SIM_TICK_SEC
                l['throughput'] = (1.0 - EWMA_ALPHA) * l['throughput'] + (EWMA_ALPHA * bits_sec)
                l['bytes_this_tick'] = 0
                if l['active'] > 0: l['active'] -= 1

            # 3. Move Packets
            alive = []
            speed = 0.03 * self.speed_multiplier

            for p in self.packets:
                if p['drop']: continue
                p['pct'] += speed

                # Link Utilization
                if p['hop'] < len(p['path']) - 1:
                    u, v = p['path'][p['hop']], p['path'][p['hop'] + 1]
                    key = tuple(sorted((u, v)))
                    if p['pct'] <= speed:
                        for l in self.links:
                            if l['key'] == key: l['bytes_this_tick'] += PACKET_SIZE_BYTES; l['active'] = 5; break

                # Arrival at Node
                if p['pct'] >= 1.0:
                    p['pct'] = 0.0

                    # Current Node
                    curr_node_id = p['path'][p['hop']]
                    target_id = p['path'][-1]

                    if curr_node_id == target_id:
                        # Reached End of Path
                        if p['type'] == 'DATA':
                            # Generate ACK at Receiver
                            ack = p.copy()
                            ack['type'] = 'ACK'
                            ack['ack'] = True
                            ack['src'] = p['dst']  # Swap
                            ack['dst'] = p['src']
                            ack['path'] = p['path'][::-1]  # Reverse Path
                            ack['hop'] = 0;
                            ack['pct'] = 0.0
                            ack['id'] = self.pkt_ctr + 90000
                            ack['acked_id'] = p['id']
                            alive.append(ack)

                        elif p['type'] == 'ACK':
                            # ACK Reached Sender
                            if p['dst'] in self.nodes:
                                self.nodes[p['dst']].on_ack(p['ev'], p['ecn'])
                        continue  # Packet Consumed

                    # Hop Logic
                    if p['hop'] < len(p['path']) - 1:
                        next_id = p['path'][p['hop'] + 1]
                        curr_obj = self.nodes.get(curr_node_id)
                        next_obj = self.nodes.get(next_id)

                        if isinstance(curr_obj, REPSSwitch): curr_obj.dequeue(next_id)

                        if isinstance(next_obj, REPSSwitch):
                            # Look ahead
                            if p['hop'] + 2 < len(p['path']):
                                outgoing = p['path'][p['hop'] + 2]
                                if not next_obj.enqueue(outgoing):
                                    p['drop'] = True;
                                    continue
                                if next_obj.check_ecn(outgoing):
                                    p['ecn'] = True

                        p['hop'] += 1
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
                nd[nid] = d

            pd = []
            for p in self.packets:
                if not p['drop']:
                    try:
                        u = p['path'][p['hop']]
                        v = p['path'][min(p['hop'] + 1, len(p['path']) - 1)]
                        pd.append({'id': p['id'], 'u': u, 'v': v, 'pct': p['pct'], 'ev': p['ev'], 'ack': p['ack'],
                                   'ecn': p['ecn'], 'acked_id': p.get('acked_id')})
                    except:
                        pass

            ld = []
            for l in self.links:
                cap_bps = l['bw'] * 1e9
                util = (l['throughput'] / cap_bps) * 100.0 if cap_bps > 0 else 0.0
                ld.append({'u': l['u'], 'v': l['v'], 'bw': l['bw'], 'util': util, 'active': l['active']})
            return {'nodes': nd, 'links': ld, 'packets': pd}


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
    <title>REPS Final</title>
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
        #modal-overlay { display:none; position:absolute; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.2); z-index:90; }
        #controls { position:absolute; bottom:20px; right:20px; z-index:10; display:flex; gap:10px; align-items:center; }
        .ctrl-btn { background:#333; color:#fff; border:none; padding:10px 20px; border-radius:4px; font-weight:bold; cursor:pointer; }
        .slider-container { background:rgba(255,255,255,0.9); padding:10px; border-radius:4px; display:flex; align-items:center; gap:10px; }
        input[type=range] { width: 100px; }
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

    <div id="modal-overlay"></div>
    <div id="link-modal">
        <h4>Link Config</h4>
        <div class="inp-group"><label>BW (Gbps):</label><input id="link-bw" type="number" value="10"></div>
        <input type="hidden" id="lu"><input type="hidden" id="lv">
        <div style="text-align:right; margin-top:15px;"><button onclick="saveLink()" style="background:#2980b9; color:#fff; border:none; padding:5px; border-radius:4px;">Save</button></div>
    </div>
    <canvas id="c"></canvas>
    <script>
        const socket = io();
        const cvs = document.getElementById('c');
        const ctx = cvs.getContext('2d');
        let W, H, state = null;
        let linkHitboxes = [];
        const NODE_WIDTH = 80, NODE_HEIGHT = 40;
        const PORT_RADIUS = 3.5; 

        function resize() { W=window.innerWidth; H=window.innerHeight; cvs.width=W; cvs.height=H; }
        window.addEventListener('resize', resize); resize();

        socket.on('connect', () => { console.log("Connected"); });
        socket.on('frame', d => { state = d; });
        function emit(cmd, val) { socket.emit('ctrl', {cmd, val}); }
        function apply() { emit('reset', { num_cores: document.getElementById('inp-c').value, num_pods: document.getElementById('inp-p').value, aggs_per_pod: document.getElementById('inp-a').value, edges_per_pod: document.getElementById('inp-e').value, hosts_per_edge: document.getElementById('inp-h').value, queue_cap: document.getElementById('inp-q').value }); }

        cvs.addEventListener('click', e => {
            const r = cvs.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top;
            for(let b of linkHitboxes) { if(pointToLineDist(mx, my, b.x1, b.y1, b.x2, b.y2) < 10) { openModal(b.u, b.v, b.bw); return; } }
        });
        function pointToLineDist(x, y, x1, y1, x2, y2) { const A=x-x1, B=y-y1, C=x2-x1, D=y2-y1; const dot=A*C+B*D, len_sq=C*C+D*D; let param=-1; if(len_sq!=0) param=dot/len_sq; let xx, yy; if(param<0){xx=x1;yy=y1}else if(param>1){xx=x2;yy=y2}else{xx=x1+param*C;yy=y1+param*D} return Math.sqrt(Math.pow(x-xx,2)+Math.pow(y-yy,2)); }
        function openModal(u, v, bw) { document.getElementById('lu').value=u; document.getElementById('lv').value=v; document.getElementById('link-bw').value=bw; document.getElementById('modal-overlay').style.display='block'; document.getElementById('link-modal').style.display='block'; }
        function saveLink() { emit('set_link_bw', { u: document.getElementById('lu').value, v: document.getElementById('lv').value, bw: document.getElementById('link-bw').value }); document.getElementById('modal-overlay').style.display='none'; document.getElementById('link-modal').style.display='none'; }

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
            linkHitboxes = [];

            // 1. Draw Links
            state.links.forEach(l => {
                const n1 = state.nodes[l.u], n2 = state.nodes[l.v];
                const p1 = {x:n1.x/100*W, y:n1.y/100*H}, p2 = {x:n2.x/100*W, y:n2.y/100*H};

                ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);
                ctx.strokeStyle = l.active > 0 ? '#3498db' : '#bdc3c7'; ctx.lineWidth = l.active > 0 ? 3 : 2; ctx.stroke();
                linkHitboxes.push({u:l.u, v:l.v, x1:p1.x, y1:p1.y, x2:p2.x, y2:p2.y, bw:l.bw});

                // Label logic
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

            // 2. Nodes
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
                    ctx.fillStyle='#34495e'; ctx.fillRect(cx-12, cy-8, 24, 16);
                    ctx.fillStyle='#fff'; ctx.font="9px Segoe UI"; ctx.textAlign="center"; ctx.fillText(id.split('_')[2], cx, cy+4);
                }
            });

            // 3. Port Circles
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

            // 4. COMPACT PACKETS
            state.packets.forEach(p => {
                const n1=state.nodes[p.u], n2=state.nodes[p.v];
                const p1={x:n1.x/100*W, y:n1.y/100*H}, p2={x:n2.x/100*W, y:n2.y/100*H};
                const x = p1.x + (p2.x-p1.x)*p.pct; const y = p1.y + (p2.y-p1.y)*p.pct;

                ctx.save(); ctx.translate(x,y);

                // COMPACT SIZES
                // Data: 36x48, ACK: 30x40
                const w = p.ack ? 30 : 36; 
                const h = p.ack ? 40 : 48;

                // Colors
                let bgColor = '#2980b9'; // Blue
                if (p.ack) bgColor = '#27ae60'; // Green
                if (p.ecn) bgColor = '#c0392b'; // Red

                ctx.fillStyle = bgColor; 
                ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5;
                ctx.beginPath(); ctx.roundRect(-w/2, -h/2, w, h, 4);
                ctx.fill(); ctx.stroke();

                // Text Config
                ctx.fillStyle = '#fff'; 
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";
                ctx.font = "bold 9px Consolas";

                const idStr = p.ack ? `A:${p.acked_id%100}` : `P:${p.id%100}`;

                // Tighter Spacing
                ctx.fillText(idStr, 0, -12);
                ctx.fillText(`E:${p.ev}`, 0, 0);
                ctx.fillText(`C:${p.ecn?1:0}`, 0, 12);

                ctx.restore();
            });
            requestAnimationFrame(draw);
        }

        function drawLabelAt(lx, ly, util, bw) {
            const x = lx + 22; 
            const y = ly;      

            ctx.fillStyle = 'rgba(255,255,255,0.9)'; 
            ctx.fillRect(x-22, y-10, 44, 20); 
            ctx.strokeStyle = '#95a5a6'; ctx.lineWidth=1; 
            ctx.strokeRect(x-22, y-10, 44, 20);

            ctx.fillStyle = '#000'; ctx.font="bold 10px Segoe UI"; ctx.textAlign="center";
            ctx.fillText(`${util.toFixed(0)}%`, x, y); 
            ctx.fillText(`${bw}Gbps`, x, y + 9); 
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
