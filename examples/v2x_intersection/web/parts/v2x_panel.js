  <script>
    // Vehicles panel: /api/v2x (web/server.py) — live positions feed, the running
    // episode's vehicles.csv / v2x_rx.csv tail and recent results.
    // Fixed colours per entity (dataviz reference palette, dark slots): A blue, B orange.
    const V2X_COLORS = {A:'#3987e5', B:'#d95926', target:'#c3c2b7', grid:'#24384a', axis:'#8ca4b5'};
    const STATE_COLORS = {CRUISE:'#1f6f4a', WARNING:'#8a6a12', BRAKING:'#8c2f3c', STOP:'#6b2230', PASSED:'#2a4a66'};
    window.v2xLatest = null;
    $('v2xStateLegend').innerHTML = Object.entries(STATE_COLORS)
      .map(([name,color]) => `<span><i style="background:${color}"></i>${name}</span>`).join('');
    function v2xAxes(ctx, W, H, x0, x1, y0, y1, pad) {
      ctx.clearRect(0,0,W,H); ctx.font='11px ui-sans-serif,system-ui'; ctx.fillStyle=V2X_COLORS.axis; ctx.strokeStyle=V2X_COLORS.grid; ctx.lineWidth=1;
      const sx=t=>pad.l+(t-x0)/(x1-x0||1)*(W-pad.l-pad.r), sy=v=>H-pad.b-(v-y0)/(y1-y0||1)*(H-pad.t-pad.b);
      const step=(y1-y0)>20?10:(y1-y0)>8?2:1;
      for(let v=Math.ceil(y0/step)*step; v<=y1; v+=step){ctx.beginPath();ctx.moveTo(pad.l,sy(v));ctx.lineTo(W-pad.r,sy(v));ctx.stroke();ctx.fillText(String(v),4,sy(v)+4);}
      for(let t=Math.ceil(x0); t<=x1; t+=2){ctx.fillText(`${t}s`,sx(t)-6,H-4);}
      return {sx,sy};
    }
    function v2xLine(ctx, pts, sx, sy, color, dash=[]) {
      if(pts.length<2)return; ctx.save(); ctx.strokeStyle=color; ctx.lineWidth=2; ctx.setLineDash(dash); ctx.beginPath();
      pts.forEach(([t,v],i)=>i?ctx.lineTo(sx(t),sy(v)):ctx.moveTo(sx(t),sy(v))); ctx.stroke(); ctx.restore();
    }
    function v2xCanvas(id){const c=$(id),r=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1;c.width=Math.max(300,r.width)*dpr;c.height=200*dpr;const ctx=c.getContext('2d');ctx.scale(dpr,dpr);return [ctx,Math.max(300,r.width),200];}
    function drawV2xCharts(d) {
      const s=d.series||[], pad={l:30,r:8,t:8,b:18};
      const x0=s.length?s[0].t:0, x1=Math.max(x0+10,s.length?s[s.length-1].t:10);
      let [ctx,W,H]=v2xCanvas('v2xSpeedChart'); let {sx,sy}=v2xAxes(ctx,W,H,x0,x1,0,16,pad);
      v2xLine(ctx,s.map(p=>[p.t,p.target]),sx,sy,V2X_COLORS.target,[5,4]);
      v2xLine(ctx,s.map(p=>[p.t,p.b_speed]),sx,sy,V2X_COLORS.B);
      v2xLine(ctx,s.map(p=>[p.t,p.a_speed]),sx,sy,V2X_COLORS.A);
      [ctx,W,H]=v2xCanvas('v2xAccelChart'); ({sx,sy}=v2xAxes(ctx,W,H,x0,x1,-6,3,pad));
      for(let i=0;i<s.length-1;i++){ctx.fillStyle=STATE_COLORS[s[i].state]||'#333';ctx.globalAlpha=.55;ctx.fillRect(sx(s[i].t),H-pad.b-6,Math.max(1,sx(s[i+1].t)-sx(s[i].t)),6);}
      ctx.globalAlpha=1; ctx.strokeStyle=V2X_COLORS.axis; ctx.beginPath(); ctx.moveTo(pad.l,sy(0)); ctx.lineTo(W-pad.r,sy(0)); ctx.stroke();
      v2xLine(ctx,s.map(p=>[p.t,p.accel]),sx,sy,V2X_COLORS.A);
      const rx=d.rx||[], ymax=Math.max(20,...rx.map(r=>r.one_way_ms))*1.1;
      [ctx,W,H]=v2xCanvas('v2xRxChart'); ({sx,sy}=v2xAxes(ctx,W,H,x0,x1,0,Math.ceil(ymax),pad));
      ctx.fillStyle=V2X_COLORS.A; rx.forEach(r=>{ctx.beginPath();ctx.arc(sx(r.t),sy(Math.min(r.one_way_ms,ymax)),3,0,7);ctx.fill();});
    }
    function renderV2x(d) {
      window.v2xLatest=d;
      const now=d.now, live=d.live||{}, frame=live.frame||{};
      $('v2xEpisode').textContent=d.episode?`${d.episode}${d.running?' · running':' · finished'}`:'—';
      $('v2xRun').textContent=d.run||'waiting for an episode';
      const state=now?.controller_state||'—'; $('v2xState').textContent=state; $('v2xState').style.background=STATE_COLORS[state]||'#1a2a38';
      const A=frame.vehicles?.A, B=frame.vehicles?.B, useLive=live.connected&&!live.stale;
      $('v2xASpeed').textContent=fmt((useLive?A?.speed_mps:now?.speed_mps)*3.6,1);
      $('v2xBSpeed').textContent=fmt((useLive?B?.speed_mps:d.b_now?.speed_mps)*3.6,1);
      $('v2xAccel').textContent=fmt(now?.accel_applied_mps2,2);
      $('v2xDist').textContent=fmt(now?.d_a_m,1);
      $('v2xInfo').textContent=now?.info||'—';
      const rx=d.rx||[]; $('v2xOneWay').textContent=rx.length?fmt(rx[rx.length-1].one_way_ms,1):'—'; $('v2xRx').textContent=rx.length;
      $('v2xFeed').textContent=live.connected?fmt(live.age_ms,0):'—';
      $('v2xResults').innerHTML=(d.results||[]).slice().reverse().map(r=>`<tr><td>${esc(r.episode)}</td><td>${esc(r.mode)}</td><td class="${r.collision?'bad-cell':''}">${r.collision?'COLLISION':esc(r.ended)}</td><td>${fmt(r.min_centre_gap_m,1)}</td><td>${fmt(r.first_risk_s,2)}</td><td>${fmt(r.first_brake_s,2)}</td><td>${esc(r.v2x_received)}</td><td>${fmt(r.one_way_ms_p50,1)}</td><td>${fmt(r.rtf,3)}</td></tr>`).join('')||'<tr><td colspan="9" class="empty">No finished episode yet</td></tr>';
      drawV2xCharts(d);
    }
    let v2xInFlight=false;
    async function pollV2x(){
      if(v2xInFlight)return; v2xInFlight=true;
      try{const r=await fetch('/api/v2x',{cache:'no-store'});if(r.ok)renderV2x(await r.json())}catch(e){}
      finally{v2xInFlight=false}
    }
    fetch('/api/road').then(r=>r.json()).then(road=>{$('v2xSumo').textContent=`· ${road.sumo_version}`}).catch(()=>{});
    pollV2x(); setInterval(pollV2x,200);
  </script>
