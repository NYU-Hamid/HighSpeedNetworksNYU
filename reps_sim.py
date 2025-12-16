import threading
import time
import random
import networkx as nx
import json
import logging
import webbrowser  # <--- Added missing import
from flask import Flask, Response, request
from flask_socketio import SocketIO, emit

# Configure logging to see exactly what's happening
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==============================================================================
#  LAYER 1: DATA PLANE & CONTROL PLANE (PYTHON BACKEND)
# ==============================================================================
class REPSSwitch:
    """Represents a switch with output queues per port."""

    def __init__(self, id, layer, capacity=4):
        self.id = id
        self.layer = layer
        self.capacity = capacity
        self.queues = {}  # Map[next_hop_id] -> count
        self.ports = {}  # Map[neighbor_id] -> port_index (0,1,2,3...)

    def enqueue(self, next_hop):
        """Returns True if enqueued, False if dropped (Tail Drop)."""
        if next_hop not in self.queues: self.queues[next_hop] = 0
        if self.queues[next_hop] >= self.capacity:
            return False  # Drop
        self.queues[next_hop] += 1
        return True

    def dequeue(self, next_hop):
        if next_hop in self.queues and self.queues[next_hop] > 0:
            self.queues[next_hop] -= 1

    def get_load(self):
        """Returns total buffer occupancy."""
        return sum(self.queues.values())

    def check_ecn(self, threshold=2):
        """Returns True if congestion is detected."""
        return self.get_load() >= threshold


class REPSHost:
    """End-host implementing the REPS selection logic."""

    def __init__(self, id):
        self.id = id
        self.ev_cache = [{'val': random.randint(10, 99), 'valid': True} for _ in range(8)]
        self.ptr = 0

    def get_valid_ev(self):
        """Round-robin search for a valid Entropy Value."""
        start = self.ptr
        for _ in range(8):
            idx = (self.ptr + _) % 8
            if self.ev_cache[idx]['valid']:
                self.ptr = (idx + 1) % 8
                return self.ev_cache[idx]['val']
        return 99  # Fallback

    def invalidate_ev(self, ev_val):
        """REPS Feedback Loop: Invalidating congested paths."""
        for entry in self.ev_cache:
            if entry['val'] == ev_val:
                entry['valid'] = False
                # Auto-heal after some time (simulating protocol timeout)
                threading.Timer(5.0, self._heal, args=[entry]).start()
                break

    def _heal(self, entry):
        entry['valid'] = True
        entry['val'] = random.randint(10, 99)


class NetworkSimulation:
    def __init__(self):
        self.graph = nx.Graph()
        self.nodes = {}  # Map[id] -> Node Obj (Switch or Host)
        self.links = []  # List of dicts
        self.packets = []
        self.lock = threading.Lock()
        self.pkt_counter = 0
        self.running = False

        self._build_topology()

    def _build_topology(self):
        """Builds a Fat-Tree Topology (2 Core, 2 Pods)."""
        # 1. Cores
        for i in range(1, 3):
            cid = f"C{i}"
            self.nodes[cid] = REPSSwitch(cid, 'core')
            self.graph.add_node(cid, type='core', y=10, x=35 + (i - 1) * 30)

        # 2. Pods
        for p in range(2):
            offset = 50 * p

            # Aggs
            for i in range(1, 3):
                aid = f"A{p}_{i}"
                self.nodes[aid] = REPSSwitch(aid, 'agg')
                self.graph.add_node(aid, type='agg', y=40, x=15 + offset + (i - 1) * 20)
                # Connect to Cores
                for c in range(1, 3): self._add_link(aid, f"C{c}")

            # Edges
            for i in range(1, 3):
                eid = f"E{p}_{i}"
                self.nodes[eid] = REPSSwitch(eid, 'edge')
                self.graph.add_node(eid, type='edge', y=70, x=15 + offset + (i - 1) * 20)
                # Connect to Aggs
                for a in range(1, 3): self._add_link(eid, f"A{p}_{a}")

                # Hosts
                for h in range(3):
                    hid = f"H{p}_{i}_{h}"
                    self.nodes[hid] = REPSHost(hid)
                    # Visual offsets for hosts
                    hx = (15 + offset + (i - 1) * 20) + (h * 4 - 4)
                    self.graph.add_node(hid, type='host', y=90, x=hx)
                    self._add_link(hid, eid)

    def _add_link(self, u, v):
        self.graph.add_edge(u, v)
        self.links.append({'u': u, 'v': v, 'active': 0})
        # Map ports for switches
        if isinstance(self.nodes.get(u), REPSSwitch):
            self.nodes[u].ports[v] = len(self.nodes[u].ports)
        if isinstance(self.nodes.get(v), REPSSwitch):
            self.nodes[v].ports[u] = len(self.nodes[v].ports)

    def step(self):
        """Discrete Time Step of the Network."""
        with self.lock:
            # A. Traffic Generation
            if random.random() < 0.3:
                hosts = [n for n in self.nodes.values() if isinstance(n, REPSHost)]
                if len(hosts) > 1:
                    src, dst = random.sample(hosts, 2)
                    ev = src.get_valid_ev()

                    try:
                        # REPS Routing: Hash EV to path
                        paths = list(nx.all_shortest_paths(self.graph, src.id, dst.id))
                        path = paths[ev % len(paths)]

                        self.pkt_counter += 1
                        self.packets.append({
                            'id': self.pkt_counter,
                            'type': 'DATA',
                            'src': src.id, 'dst': dst.id,
                            'ev': ev,
                            'path': path, 'hop_idx': 0, 'pct': 0.0,  # Visual interpolation
                            'ecn': False, 'ack': False, 'drop': False
                        })
                    except:
                        pass

            # B. Link Decay
            for l in self.links:
                if l['active'] > 0: l['active'] -= 1

            # C. Packet Processing
            alive_packets = []
            for p in self.packets:
                if p['drop']: continue

                # Interpolation Speed
                p['pct'] += 0.025  # 40 frames per hop

                # Visual: Activate Link
                if p['hop_idx'] < len(p['path']) - 1:
                    u, v = p['path'][p['hop_idx']], p['path'][p['hop_idx'] + 1]
                    # Find link
                    for l in self.links:
                        if (l['u'] == u and l['v'] == v) or (l['u'] == v and l['v'] == u):
                            l['active'] = 2
                            break

                # Hop Complete
                if p['pct'] >= 1.0:
                    p['pct'] = 0.0
                    curr_node_id = p['path'][p['hop_idx']]

                    # 1. Check if Reached Destination
                    target = p['src'] if p['ack'] else p['dst']
                    if curr_node_id == target:
                        if not p['ack']:
                            # Turn into ACK
                            ack = p.copy()
                            ack['ack'] = True;
                            ack['type'] = 'ACK'
                            ack['path'] = p['path'][::-1]
                            ack['hop_idx'] = 0;
                            ack['pct'] = 0.0
                            ack['id'] = self.pkt_counter + 90000
                            alive_packets.append(ack)
                        else:
                            # Reached Source: Process ECN
                            if p['ecn']:
                                self.nodes[p['src']].invalidate_ev(p['ev'])
                        continue

                    # 2. Move to Next Hop
                    if p['hop_idx'] < len(p['path']) - 1:
                        next_hop_id = p['path'][p['hop_idx'] + 1]

                        # --- SWITCH LOGIC ---
                        # Dequeue from current
                        curr_node = self.nodes.get(curr_node_id)
                        if isinstance(curr_node, REPSSwitch):
                            curr_node.dequeue(next_hop_id)

                        # Enqueue to next
                        next_node = self.nodes.get(next_hop_id)
                        if isinstance(next_node, REPSSwitch):
                            # Buffer Check
                            if not next_node.enqueue(curr_node_id):  # Tail Drop
                                p['drop'] = True
                                continue
                                # ECN Check
                            if next_node.check_ecn():
                                p['ecn'] = True

                        p['hop_idx'] += 1
                        alive_packets.append(p)
                else:
                    alive_packets.append(p)

            self.packets = alive_packets

    def get_state(self):
        """Serialize State for Frontend."""
        with self.lock:
            # Nodes
            nodes_data = {}
            for nid, node in self.nodes.items():
                meta = self.graph.nodes[nid]
                data = {'x': meta['x'], 'y': meta['y'], 'type': meta['type']}
                if isinstance(node, REPSSwitch):
                    data['q'] = node.get_load()
                    data['cap'] = node.capacity
                if isinstance(node, REPSHost):
                    # Only send valid count to save bandwidth, or minimal data
                    data['valid_evs'] = sum(1 for e in node.ev_cache if e['valid'])
                nodes_data[nid] = data

            # Packets
            pkts_data = []
            for p in self.packets:
                if p['drop']: continue
                u = p['path'][p['hop_idx']]
                v = p['path'][min(p['hop_idx'] + 1, len(p['path']) - 1)]
                pkts_data.append({
                    'id': p['id'], 'u': u, 'v': v, 'pct': p['pct'],
                    'ev': p['ev'], 'ack': p['ack'], 'ecn': p['ecn']
                })

            return {'nodes': nodes_data, 'links': self.links, 'packets': pkts_data}


# ==============================================================================
#  LAYER 2: PRESENTATION SERVER (FLASK)
# ==============================================================================
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
sim = NetworkSimulation()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>REPS Engineering View</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { margin:0; background:#ffffff; font-family:'Courier New', monospace; overflow:hidden; }
        #canvas-wrapper { width:100vw; height:100vh; position:relative; }
        canvas { display:block; }

        #hud {
            position:absolute; top:20px; left:20px;
            background:rgba(255,255,255,0.9); padding:15px; border:2px solid #333;
            box-shadow: 4px 4px 0px #333;
        }
        h1 { margin:0; font-size:20px; text-transform:uppercase; color:#333; }
        .status { margin-top:5px; font-size:12px; color:#555; }

        #controls {
            position:absolute; bottom:20px; right:20px;
            display:flex; gap:10px;
        }
        button {
            background:#fff; border:2px solid #333; padding:10px 20px;
            font-family:inherit; font-weight:bold; cursor:pointer;
            box-shadow: 2px 2px 0px #333; transition:transform 0.1s;
        }
        button:active { transform:translate(2px, 2px); box-shadow:none; }
        button.active { background:#333; color:#fff; }

        /* Legend */
        .legend-item { display:flex; align-items:center; gap:8px; font-size:12px; margin-top:5px;}
        .sw { width:12px; height:8px; border:1px solid #333; background:#87CEEB; }
        .path { width:12px; height:2px; background:#00BFFF; }
        .pkt { width:8px; height:8px; border:1px solid #006400; background:#fff; }
    </style>
</head>
<body>
    <div id="canvas-wrapper">
        <canvas id="c"></canvas>

        <div id="hud">
            <h1>REPS Protocol Analyzer</h1>
            <div class="status" id="conn-status">Status: Connecting...</div>
            <div style="margin-top:10px; border-top:1px solid #ccc; padding-top:5px;">
                <div class="legend-item"><div class="sw"></div> Switch (Queue Visual)</div>
                <div class="legend-item"><div class="path"></div> Active Path (SkyBlue)</div>
                <div class="legend-item"><div class="pkt" style="border-color:red; background:#ffebeb"></div> ECN Marked Packet</div>
            </div>
        </div>

        <div id="controls">
            <button onclick="emit('start')" id="btn-start">START SIMULATION</button>
            <button onclick="emit('pause')">PAUSE</button>
        </div>
    </div>

    <script>
        const socket = io();
        const cvs = document.getElementById('c');
        const ctx = cvs.getContext('2d');
        const statusDiv = document.getElementById('conn-status');
        let W, H;
        let state = null; // Stores the latest frame from Python

        // Init Canvas
        function resize() { 
            W = window.innerWidth; 
            H = window.innerHeight; 
            cvs.width = W; 
            cvs.height = H; 
        }
        window.addEventListener('resize', resize);
        resize();

        // Socket Events
        socket.on('connect', () => { statusDiv.innerText = "Status: Connected to Backend"; });
        socket.on('disconnect', () => { statusDiv.innerText = "Status: Disconnected"; });
        socket.on('frame', (data) => { state = data; });

        function emit(cmd) { socket.emit('ctrl', {cmd}); }

        // --- VISUALIZATION ENGINE ---
        // Helper: Calculate Port Position for Wires
        function getPort(n, targetNode) {
            // Percent to Pixels
            const nx = n.x/100 * W;
            const ny = n.y/100 * H;

            if (n.type === 'host') return {x: nx, y: ny - 10};

            // Switch: Connects from edges
            const tx = targetNode.x/100 * W;
            const ty = targetNode.y/100 * H;

            let ox = 0, oy = 0;
            // Simple logic: if target is above, connect top; if below, connect bottom
            if (ty < ny) oy = -15; else oy = 15;
            // Spread horizontal ports
            if (tx < nx) ox = -10; else if (tx > nx) ox = 10;

            return {x: nx + ox, y: ny + oy};
        }

        function draw() {
            ctx.clearRect(0, 0, W, H);

            if (!state) {
                ctx.fillStyle = "#999"; ctx.textAlign = "center"; ctx.font = "14px monospace";
                ctx.fillText("Waiting for Simulation Data...", W/2, H/2);
                requestAnimationFrame(draw);
                return;
            }

            // 1. LINKS
            ctx.lineCap = "round";
            state.links.forEach(l => {
                const u = state.nodes[l.u], v = state.nodes[l.v];
                const p1 = getPort(u, v);
                const p2 = getPort(v, u);

                ctx.beginPath();
                ctx.moveTo(p1.x, p1.y);
                ctx.lineTo(p2.x, p2.y);

                if (l.active > 0) {
                    ctx.strokeStyle = "#00BFFF"; // Deep Sky Blue
                    ctx.lineWidth = 3;
                } else {
                    ctx.strokeStyle = "#ccc";
                    ctx.lineWidth = 1;
                }
                ctx.stroke();
            });

            // 2. NODES
            Object.entries(state.nodes).forEach(([id, n]) => {
                const cx = n.x/100 * W;
                const cy = n.y/100 * H;

                if (n.type !== 'host') {
                    // Switch: Rectangular
                    const w = 50, h = 30;
                    ctx.fillStyle = (n.type==='core') ? "#4682B4" : "#87CEEB";
                    ctx.strokeStyle = "#000"; ctx.lineWidth = 2;

                    ctx.beginPath();
                    ctx.rect(cx - w/2, cy - h/2, w, h);
                    ctx.fill(); ctx.stroke();

                    // Queue Visualization (Bar on the right)
                    const qh = 24;
                    const qx = cx + w/2 + 4;
                    ctx.fillStyle = "#eee"; ctx.fillRect(qx, cy - qh/2, 6, qh);
                    ctx.strokeRect(qx, cy - qh/2, 6, qh);

                    // Fill level
                    if (n.q > 0) {
                        const fillH = (n.q / n.cap) * qh;
                        ctx.fillStyle = (n.q >= 2) ? "red" : "lime"; // ECN Threshold Color
                        ctx.fillRect(qx, (cy + qh/2) - fillH, 6, fillH);
                    }

                    // Ports
                    ctx.fillStyle = "white"; ctx.strokeStyle="black"; ctx.lineWidth=1;
                    [-10, 10].forEach(ox => {
                         [-15, 15].forEach(oy => {
                             ctx.beginPath(); ctx.arc(cx+ox, cy+oy, 3, 0, Math.PI*2); ctx.fill(); ctx.stroke();
                         });
                    });

                    // Label
                    ctx.fillStyle = "black"; ctx.textAlign="center"; ctx.font="bold 10px monospace";
                    ctx.fillText(id, cx, cy+4);

                } else {
                    // Host
                    ctx.fillStyle = "#333";
                    ctx.fillRect(cx - 10, cy - 10, 20, 20);
                    ctx.fillStyle = "#ccc"; ctx.font="9px monospace";
                    ctx.fillText(id.split('_')[2], cx, cy+4);
                }
            });

            // 3. PACKETS
            state.packets.forEach(p => {
                const u = state.nodes[p.u], v = state.nodes[p.v];
                const p1 = getPort(u, v);
                const p2 = getPort(v, u);

                // Lerp
                const x = p1.x + (p2.x - p1.x) * p.pct;
                const y = p1.y + (p2.y - p1.y) * p.pct;

                ctx.save();
                ctx.translate(x, y);

                // Box
                const pw = 36, ph = 20;
                ctx.fillStyle = p.ack ? "#E6E6FA" : (p.ecn ? "#ffebeb" : "white");
                ctx.strokeStyle = p.ack ? "purple" : (p.ecn ? "red" : "green");
                ctx.lineWidth = 2;

                ctx.beginPath();
                ctx.rect(-pw/2, -ph/2, pw, ph);
                ctx.fill(); ctx.stroke();

                // Text
                ctx.fillStyle = "black"; ctx.font = "9px monospace"; ctx.textAlign="center";
                ctx.fillText("EV:"+p.ev, 0, 3);

                // Header (Tiny ID)
                ctx.fillStyle = p.ecn ? "red" : "green";
                ctx.fillRect(-pw/2, -ph/2, pw, 4);

                ctx.restore();
            });

            requestAnimationFrame(draw);
        }

        requestAnimationFrame(draw);
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    return Response(HTML_TEMPLATE, mimetype='text/html')


@socketio.on('ctrl')
def handle_ctrl(msg):
    logger.info(f"Received Command: {msg}")
    if msg['cmd'] == 'start':
        sim.running = True
    elif msg['cmd'] == 'pause':
        sim.running = False


def simulation_loop():
    logger.info("Simulation Loop Started")
    while True:
        if sim.running:
            sim.step()

        # Always emit state, even if paused, so connection is confirmed visually
        socketio.emit('frame', sim.get_state())
        time.sleep(0.02)  # 50 FPS


if __name__ == '__main__':
    # Start Simulation Thread
    t = threading.Thread(target=simulation_loop, daemon=True)
    t.start()

    logger.info("Starting Web Server on Port 5000...")
    webbrowser.open("http://127.0.0.1:5000")
    socketio.run(app, port=5000, allow_unsafe_werkzeug=True)
