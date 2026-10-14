  <script type="importmap">
    {"imports":{"three":"/vendor/three.module.min.js"}}
  </script>
  <script type="module">
    // The intersection as SUMO builds it (/api/road: lane and junction shapes
    // from sumolib, the layout's blocks), the vehicles from the live positions
    // feed (/api/vehicles, interpolated per frame), and the radio from the
    // Sionna status (gNB position, rays of gnb0 -> ue0, strongest DL tap for
    // A's trail). Scene metres, Z up, exactly as SUMO and Sionna use them.
    import * as THREE from 'three';
    import { OrbitControls } from '/vendor/OrbitControls.js';
    import { Line2 } from '/vendor/Line2.js';
    import { LineGeometry } from '/vendor/LineGeometry.js';
    import { LineMaterial } from '/vendor/LineMaterial.js';

    const host = document.getElementById('scene3d');
    const statusOverlay = document.getElementById('sceneStatus');
    const COLORS = { A: 0x3987e5, B: 0xd95926 };
    const toggles = { rays: true, labels: true, trail: true, follow: false };

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xbfd3e3);
    scene.fog = new THREE.Fog(0xbfd3e3, 260, 520);
    const camera = new THREE.PerspectiveCamera(34, 1, 0.5, 2000);
    camera.up.set(0, 0, 1);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.maxPolarAngle = Math.PI * 0.48;

    scene.add(new THREE.HemisphereLight(0xe8f1ff, 0x5b5346, 0.9));
    const sun = new THREE.DirectionalLight(0xfff1dc, 2.2);
    sun.position.set(-90, -120, 160);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -170, right: 170, top: 170, bottom: -170, near: 10, far: 500 });
    sun.shadow.bias = -0.0004;
    scene.add(sun);

    const world = new THREE.Group(), dynamic = new THREE.Group(), rays = new THREE.Group();
    scene.add(world, dynamic, rays);

    const flat = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.95, metalness: 0, ...extra });
    function slab(points, z, material) {
      const shape = new THREE.Shape(points.map(([x, y]) => new THREE.Vector2(x, y)));
      const mesh = new THREE.Mesh(new THREE.ShapeGeometry(shape), material);
      mesh.position.z = z; mesh.receiveShadow = true; return mesh;
    }
    function rect(x0, x1, y0, y1, z, material) { return slab([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], z, material); }
    // A straight polyline widened to a strip of width w (lane bodies and markings).
    function strip(shape, w, z, material) {
      const g = new THREE.Group();
      for (let i = 0; i < shape.length - 1; i++) {
        const [x0, y0] = shape[i], [x1, y1] = shape[i + 1], len = Math.hypot(x1 - x0, y1 - y0);
        if (len < 1e-6) continue;
        const nx = -(y1 - y0) / len * w / 2, ny = (x1 - x0) / len * w / 2;
        g.add(slab([[x0 + nx, y0 + ny], [x1 + nx, y1 + ny], [x1 - nx, y1 - ny], [x0 - nx, y0 - ny]], z, material));
      }
      return g;
    }
    function dashed(p0, p1, w, dash, gap, z, material) {
      const g = new THREE.Group(), len = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]);
      const ux = (p1[0] - p0[0]) / len, uy = (p1[1] - p0[1]) / len;
      for (let s = 0; s < len; s += dash + gap) {
        const e = Math.min(len, s + dash);
        g.add(strip([[p0[0] + ux * s, p0[1] + uy * s], [p0[0] + ux * e, p0[1] + uy * e]], w, z, material));
      }
      return g;
    }
    function facadeTexture(seed) {
      const c = document.createElement('canvas'); c.width = 128; c.height = 128;
      const x = c.getContext('2d'); let r = seed;
      const rand = () => (r = (r * 16807) % 2147483647) / 2147483647;
      x.fillStyle = '#b8b2a7'; x.fillRect(0, 0, 128, 128);
      x.fillStyle = '#a49d92'; x.fillRect(0, 118, 128, 10);
      for (let col = 0; col < 2; col++) {
        const lit = rand() < 0.18;
        x.fillStyle = lit ? '#f4d79a' : '#3f5466';
        x.fillRect(12 + col * 64, 26, 40, 66);
        x.fillStyle = '#d9d4cb'; x.fillRect(12 + col * 64, 58, 40, 3);
      }
      const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
      t.wrapS = t.wrapT = THREE.RepeatWrapping; t.anisotropy = 8; return t;
    }
    function label(text, color = '#e9f2f8') {
      const c = document.createElement('canvas'), ctx = c.getContext('2d'), font = '600 34px ui-sans-serif,system-ui,sans-serif';
      ctx.font = font; const w = Math.ceil(ctx.measureText(text).width) + 28; c.width = w; c.height = 52;
      ctx.font = font; ctx.fillStyle = 'rgba(8,16,25,.78)'; ctx.beginPath(); ctx.roundRect(0, 0, w, 52, 14); ctx.fill();
      ctx.fillStyle = color; ctx.fillText(text, 14, 37);
      const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
      // Fixed on-screen size (sizeAttenuation off): readable at any zoom.
      const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: t, depthTest: false, sizeAttenuation: false }));
      s.scale.set(w / 52 * 0.024, 0.024, 1); s.renderOrder = 10; return s;
    }
    // Swap only the texture: a new material per text change would make
    // three.js release and recompile the sprite shader program.
    function setLabel(sprite, text, color) {
      if (sprite.userData.text === text) return;
      const fresh = label(text, color), old = sprite.material.map;
      sprite.material.map = fresh.material.map; old.dispose(); fresh.material.dispose();
      sprite.scale.copy(fresh.scale); sprite.userData.text = text;
    }

    let road = null;
    function buildWorld(r) {
      road = r;
      const e = r.ground_half_extent_m, s = r.setback_m, ext = Math.max(...r.blocks.map(b => Math.max(...b.x.map(Math.abs))));
      world.add(rect(-e, e, -e, e, -0.05, flat(0x8fae6e)));                              // grass
      world.add(rect(-ext - 6, ext + 6, -ext - 6, ext + 6, -0.03, flat(0xc9c4ba)));       // pavement / plazas
      const asphalt = flat(0x3b3e42, { roughness: 0.9 });
      for (const lane of r.lanes.filter(l => !l.internal)) world.add(strip(lane.shape, lane.width_m, 0.0, asphalt));
      for (const j of r.junctions.filter(j => j.id === 'C')) world.add(slab(j.shape, 0.001, asphalt));
      // Curbs along both sides of each road, between the asphalt and the pavement.
      const curb = flat(0xe1ddd5), half = r.lane_width_m, lim = e;
      for (const sign of [-1, 1]) {
        world.add(rect(-lim, -s, sign * half - 0.15, sign * half + 0.15, 0.02, curb));
        world.add(rect(s, lim, sign * half - 0.15, sign * half + 0.15, 0.02, curb));
        world.add(rect(sign * half - 0.15, sign * half + 0.15, -lim, -s, 0.02, curb));
        world.add(rect(sign * half - 0.15, sign * half + 0.15, s, lim, 0.02, curb));
      }
      // Markings: yellow double centre line, white dashed edge, stop lines, zebra crossings.
      const yellow = flat(0xe8c547, { roughness: 0.6 }), white = flat(0xf2f2ee, { roughness: 0.6 }), mz = 0.012;
      const mouth = s + 4.0;
      for (const [a, b] of [[[0, -mouth], [0, -lim]], [[0, mouth], [0, lim]], [[-mouth, 0], [-lim, 0]], [[mouth, 0], [lim, 0]]]) {
        const vertical = a[0] === 0, off = 0.12;
        world.add(strip([[a[0] + (vertical ? off : 0), a[1] + (vertical ? 0 : off)], [b[0] + (vertical ? off : 0), b[1] + (vertical ? 0 : off)]], 0.12, mz, yellow));
        world.add(strip([[a[0] - (vertical ? off : 0), a[1] - (vertical ? 0 : off)], [b[0] - (vertical ? off : 0), b[1] - (vertical ? 0 : off)]], 0.12, mz, yellow));
      }
      for (const line of r.stop_lines) world.add(strip([line.from, line.to], 0.45, mz, white));
      for (const [cx, cy, vertical] of [[0, -s - 1.5, true], [0, s + 1.5, true], [-s - 1.5, 0, false], [s + 1.5, 0, false]]) {
        for (let k = -3; k <= 3; k++) {
          const o = k * 0.95;
          world.add(vertical ? rect(cx + o - 0.25, cx + o + 0.25, cy - 1.4, cy + 1.4, mz, white)
                             : rect(cx - 1.4, cx + 1.4, cy + o - 0.25, cy + o + 0.25, mz, white));
        }
      }
      // Buildings: window facades, flat roofs with a parapet colour.
      r.blocks.forEach((b, i) => {
        const w = b.x[1] - b.x[0], d = b.y[1] - b.y[0], h = b.height_m;
        const tex = facadeTexture(7 + i * 131); tex.repeat.set(w / 6, h / 3.6);
        const side = new THREE.MeshStandardMaterial({ map: tex, roughness: 0.85 });
        const roof = flat(0x8f8a82);
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, d, h), [side, side, side, side, roof, roof]);
        mesh.position.set((b.x[0] + b.x[1]) / 2, (b.y[0] + b.y[1]) / 2, h / 2);
        mesh.castShadow = mesh.receiveShadow = true; world.add(mesh);
      });
      world.traverse(o => { if (o.isMesh && !o.castShadow) o.receiveShadow = true; });
      frame();
      statusOverlay.hidden = true;
      document.getElementById('sceneCredit').textContent = `${r.sumo_version} network · ${r.lanes.length} lanes`;
    }

    function makeCar(color) {
      const g = new THREE.Group(), L = road?.vehicle.length_m || 4.5, W = road?.vehicle.width_m || 1.8;
      const paint = new THREE.MeshStandardMaterial({ color, metalness: 0.45, roughness: 0.35 });
      const body = new THREE.Mesh(new THREE.BoxGeometry(L, W, 0.62), paint); body.position.z = 0.62;
      const cabin = new THREE.Mesh(new THREE.BoxGeometry(L * 0.5, W * 0.86, 0.55),
        new THREE.MeshStandardMaterial({ color: 0x1b2633, metalness: 0.2, roughness: 0.15 }));
      cabin.position.set(-L * 0.06, 0, 1.2);
      const roof = new THREE.Mesh(new THREE.BoxGeometry(L * 0.44, W * 0.82, 0.06), paint); roof.position.set(-L * 0.06, 0, 1.5);
      g.add(body, cabin, roof);
      const tyre = new THREE.MeshStandardMaterial({ color: 0x15171a, roughness: 0.8 });
      for (const sx of [1, -1]) for (const sy of [1, -1]) {
        const w = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.34, 0.26, 18), tyre);
        w.position.set(sx * L * 0.32, sy * (W / 2 - 0.08), 0.34); g.add(w);
      }
      const head = new THREE.MeshStandardMaterial({ color: 0xfff6d8, emissive: 0xfff2c0, emissiveIntensity: 0.6 });
      const brake = new THREE.MeshStandardMaterial({ color: 0x5a0d0d, emissive: 0xff2a1a, emissiveIntensity: 0.1 });
      for (const sy of [1, -1]) {
        const h = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.34, 0.14), head); h.position.set(L / 2, sy * (W / 2 - 0.3), 0.72);
        const b = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.36, 0.14), brake); b.position.set(-L / 2, sy * (W / 2 - 0.3), 0.74);
        g.add(h, b);
      }
      g.traverse(o => { if (o.isMesh) o.castShadow = true; });
      g.userData.brake = brake;
      g.userData.tag = label('—'); g.userData.tag.position.set(0, 0, 3.6); g.add(g.userData.tag);
      return g;
    }

    const cars = {};
    let vehiclesSnap = null, vehiclesAt = 0;
    async function pollVehicles() {
      try {
        const r = await fetch('/api/vehicles', { cache: 'no-store' });
        if (r.ok) { vehiclesSnap = await r.json(); vehiclesAt = performance.now(); }
      } catch (e) { /* keep the last pose */ }
    }

    // Trail of A coloured by the strongest DL tap at each Sionna update.
    const TRAIL_MAX = 400, trailPos = new Float32Array(TRAIL_MAX * 3), trailCol = new Float32Array(TRAIL_MAX * 3);
    const trailGeom = new THREE.BufferGeometry();
    trailGeom.setAttribute('position', new THREE.BufferAttribute(trailPos, 3));
    trailGeom.setAttribute('color', new THREE.BufferAttribute(trailCol, 3));
    const trail = new THREE.Points(trailGeom, new THREE.PointsMaterial({ size: 2.2, vertexColors: true, sizeAttenuation: true }));
    dynamic.add(trail);
    let trailCount = 0, lastTrailY = null, lastIteration = null;
    const gainColor = db => {  // one hue (amber), light = strong
      const t = Math.min(1, Math.max(0, (db + 36) / 24));
      return new THREE.Color().setHSL(0.11, 0.9, 0.18 + 0.6 * t);
    };

    // Ray materials: one per gain bucket, created once and never disposed, so
    // rebuilding the rays every Sionna iteration compiles no shader.
    const RAY_BUCKETS = 6;
    const rayMaterials = Array.from({ length: RAY_BUCKETS }, (_, k) => {
      const t = k / (RAY_BUCKETS - 1);
      return new LineMaterial({ color: new THREE.Color().setHSL(0.53, 0.9, 0.35 + 0.35 * t), linewidth: 1 + 3 * t,
                                transparent: true, opacity: 0.35 + 0.6 * t, depthTest: true });
    });

    let gnb = null, parked = null;
    function updateRadio(sionna) {
      const p = sionna?.positions || {};
      if (p.gnb0) {
        if (!gnb) {
          gnb = new THREE.Group();
          const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.18, 0.28, 1, 12), flat(0x8d949b, { metalness: 0.5, roughness: 0.4 }));
          mast.rotation.x = Math.PI / 2; mast.castShadow = true; gnb.add(mast);
          const panel = new THREE.Mesh(new THREE.BoxGeometry(0.35, 0.9, 1.6), flat(0xeef0f2)); panel.castShadow = true; gnb.add(panel);
          const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.22, 12, 8), new THREE.MeshStandardMaterial({ color: 0xff3b30, emissive: 0xff3b30, emissiveIntensity: 1.5 }));
          gnb.add(lamp); gnb.userData = { mast, panel, lamp, tag: label('gNB', '#ffd27a') }; gnb.add(gnb.userData.tag);
          dynamic.add(gnb);
        }
        const [x, y, z] = p.gnb0, u = gnb.userData;
        u.mast.scale.y = z; u.mast.position.set(x, y, z / 2); u.panel.position.set(x, y, z); u.lamp.position.set(x, y, z + 1.0);
        u.tag.position.set(x, y, z + 3.2);
      }
      if (p.ue1) {
        if (!parked) {
          parked = new THREE.Group();
          const box = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 1.5), flat(0x6f7c86)); box.position.z = 0.75; box.castShadow = true;
          parked.add(box); const tag = label('ue1 · parked', '#c3c2b7'); tag.position.z = 3; parked.add(tag); parked.userData.tag = tag;
          dynamic.add(parked);
        }
        parked.position.set(p.ue1[0], p.ue1[1], 0);
      }
      // Rays gnb0 -> ue0, brightness and width by gain. Rebuilt only when the
      // bridge has traced a new iteration (10 Hz), not on every 100 ms poll,
      // and the old lines are disposed so GPU buffers do not accumulate.
      const ch = (sionna?.channels || []).find(c => c.source === 'gnb0' && c.destination === 'ue0');
      const raysKey = `${sionna?.iteration}|${toggles.rays}`;
      if (raysKey !== rays.userData.key) {
        rays.userData.key = raysKey;
        for (const line of rays.children) line.geometry.dispose();
        rays.clear();
      }
      if (toggles.rays && !rays.children.length && ch?.path_polylines?.length) {
        const gains = ch.path_polylines.map(l => l.gain_db), gMax = Math.max(...gains), gMin = Math.min(...gains);
        for (const line of ch.path_polylines) {
          const t = gMax > gMin ? (line.gain_db - gMin) / (gMax - gMin) : 1;
          const geom = new LineGeometry(); geom.setPositions(line.points.flat());
          rays.add(new Line2(geom, rayMaterials[Math.round(t * (RAY_BUCKETS - 1))]));
        }
      }
      // Trail point per Sionna iteration.
      if (ch && p.ue0 && sionna.iteration !== lastIteration) {
        lastIteration = sionna.iteration;
        if (lastTrailY !== null && p.ue0[1] < lastTrailY - 20) trailCount = 0;   // new episode: A is back at the start
        lastTrailY = p.ue0[1];
        const i = trailCount % TRAIL_MAX, c = gainColor(ch.strongest_tap_gain_db);
        trailPos.set([p.ue0[0] - 1.1, p.ue0[1], 0.05], i * 3); trailCol.set([c.r, c.g, c.b], i * 3);
        trailCount++; trailGeom.setDrawRange(0, Math.min(trailCount, TRAIL_MAX));
        trailGeom.attributes.position.needsUpdate = trailGeom.attributes.color.needsUpdate = true;
        document.getElementById('sceneTap').textContent = `DL strongest tap to A ${Number(ch.strongest_tap_gain_db).toFixed(1)} dB`;
      }
      trail.visible = toggles.trail;
    }

    function updateCars(dt) {
      const frameData = vehiclesSnap?.frame, vehicles = frameData?.vehicles;
      if (!vehicles || !road) return;
      const state = window.v2xLatest?.now?.controller_state, accel = Number(window.v2xLatest?.now?.accel_applied_mps2 ?? 0);
      const ahead = Math.min(0.3, (performance.now() - vehiclesAt) / 1000 + (vehiclesSnap.age_ms || 0) / 1000);
      for (const [id, v] of Object.entries(vehicles)) {
        if (!cars[id]) { cars[id] = makeCar(COLORS[id] ?? 0x888888); dynamic.add(cars[id]); }
        const car = cars[id], yaw = v.yaw_rad, L = road.vehicle.length_m;
        const fx = v.position_m[0] + v.velocity_mps[0] * (vehiclesSnap.stale ? 0 : ahead), fy = v.position_m[1] + v.velocity_mps[1] * (vehiclesSnap.stale ? 0 : ahead);
        const target = new THREE.Vector3(fx - Math.cos(yaw) * L / 2, fy - Math.sin(yaw) * L / 2, 0);
        if (car.position.distanceTo(target) > 15) car.position.copy(target); else car.position.lerp(target, Math.min(1, dt * 12));
        car.rotation.z = yaw;
        const braking = id === 'A' ? accel < -0.5 : false;
        car.userData.brake.emissiveIntensity = braking ? 3.0 : 0.1;
        const text = id === 'A' ? `A ${(v.speed_mps * 3.6).toFixed(0)} km/h${state ? ' · ' + state : ''}` : `B ${(v.speed_mps * 3.6).toFixed(0)} km/h`;
        setLabel(car.userData.tag, text, id === 'A' ? '#9ec5f4' : '#f3b08f');
        car.userData.tag.visible = toggles.labels;
      }
    }

    function frame() {
      camera.position.set(28, -125, 235); controls.target.set(16, -22, 0); controls.update();
    }
    function resize() {
      const w = host.clientWidth, h = host.clientHeight || 560;
      renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
      rayMaterials.forEach(m => m.resolution.set(w, h));
    }
    new ResizeObserver(resize).observe(host); resize();

    const clock = new THREE.Clock();
    renderer.setAnimationLoop(() => {
      const dt = clock.getDelta();
      updateCars(dt);
      if (toggles.follow && cars.A) {
        const delta = cars.A.position.clone().sub(controls.target);
        controls.target.add(delta); camera.position.add(delta);
      }
      if (gnb) gnb.userData.lamp.visible = Math.floor(performance.now() / 600) % 2 === 0;
      parked && (parked.userData.tag.visible = toggles.labels); gnb && (gnb.userData.tag.visible = toggles.labels);
      controls.update(); renderer.render(scene, camera);
    });

    for (const [id, key] of [['sceneRaysToggle', 'rays'], ['sceneLabelsToggle', 'labels'], ['sceneTrailToggle', 'trail'], ['sceneFollowToggle', 'follow']]) {
      const button = document.getElementById(id);
      button.setAttribute('aria-pressed', String(toggles[key]));
      button.addEventListener('click', () => { toggles[key] = !toggles[key]; button.setAttribute('aria-pressed', String(toggles[key])); });
    }
    document.getElementById('sceneResetView').addEventListener('click', frame);

    fetch('/api/road').then(r => r.json()).then(buildWorld).catch(e => { statusOverlay.textContent = `road geometry unavailable: ${e}`; });
    pollVehicles(); setInterval(pollVehicles, 100);
    window.sceneViewer = { update: updateRadio, resize };
    window.v2xSceneDebug = { renderer };   // renderer.info for diagnostics
  </script>
