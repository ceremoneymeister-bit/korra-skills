/* MIT · Korra. Fluid solver derived from Pavel Dobryakov; see fluid-LICENSE.txt. */
export function mount(config, manifest, assetURL) {
  "use strict";
  var hero = document.querySelector(config.selector);
  var canvas = hero && hero.querySelector(".hero-canvas");
  var word = hero && hero.querySelector(".hero-fallback");
  if (!canvas || !word) return;
  var reduce = matchMedia("(prefers-reduced-motion: reduce)"), gl = null;

  var MATERIALS = ["lime", "clear", "opal"];                                      // lacquer per stroke
  var TINT = { lime: [0.80, 0.96, 0.32, 0.0], clear: [0.985, 0.99, 0.985, 0.0], opal: [0.99, 0.985, 0.975, 0.5] };
  var BALLOON = {mx: manifest.margins[0], my: manifest.margins[1]};
  var SVG = manifest.svg;
  var P = {
    sim: 200, dye: 1024, pressureIters: 22, curl: 0, gravity: 0.12,
    velDiss: 3.0, dyeDiss: 0.75, heal: 1.1, disp: 0,
    radius: 0.30, force: 0.45, maxSpeed: 2.5, ink: 1.9, threshold: 0.42,
    refract: 0.016, slope: 7.0, stroke: 260
  };
  var VS = "#version 300 es\nin vec2 aPos;uniform vec2 texel;out vec2 vUv,vL,vR,vT,vB;" +
    "void main(){vUv=aPos*.5+.5;vL=vUv-vec2(texel.x,0.);vR=vUv+vec2(texel.x,0.);vT=vUv+vec2(0.,texel.y);vB=vUv-vec2(0.,texel.y);gl_Position=vec4(aPos,0.,1.);}";
  var HEAD = "#version 300 es\nprecision highp float;precision highp sampler2D;in vec2 vUv,vL,vR,vT,vB;out vec4 o;";
  var FS = {
    splat: "uniform sampler2D src;uniform vec2 a,b;uniform float aspect,radius;uniform vec4 value;uniform float cap;" +
      "void main(){vec2 p=vUv,A=a,B=b;p.x*=aspect;A.x*=aspect;B.x*=aspect;vec2 pa=p-A,ba=B-A;" +
      "float h=clamp(dot(pa,ba)/max(dot(ba,ba),1e-9),0.,1.);float d=length(pa-ba*h);" +
      "vec4 v=texture(src,vUv)+value*exp(-d*d/radius);o=cap>0.?min(v,vec4(cap)):v;}",
    curl: "uniform sampler2D vel;void main(){float L=texture(vel,vL).y,R=texture(vel,vR).y,T=texture(vel,vT).x,B=texture(vel,vB).x;o=vec4(.5*(R-L-T+B),0.,0.,1.);}",
    vort: "uniform sampler2D vel,curlT,dye;uniform float curl,dt,gravity;" +
      "void main(){float L=texture(curlT,vL).x,R=texture(curlT,vR).x,T=texture(curlT,vT).x,B=texture(curlT,vB).x,C=texture(curlT,vUv).x;" +
      "vec2 f=.5*vec2(abs(T)-abs(B),abs(R)-abs(L));f/=length(f)+1e-4;f*=curl*C;f.y=-f.y;" +
      "vec2 v=texture(vel,vUv).xy+f*dt;vec4 d=texture(dye,vUv);float ink=clamp(d.r+d.g+d.b+d.a,0.,1.6);" +
      "v.y-=gravity*ink*dt*60.;o=vec4(clamp(v,-1500.,1500.),0.,1.);}",
    div: "uniform sampler2D vel;void main(){vec2 C=texture(vel,vUv).xy;float L=texture(vel,vL).x,R=texture(vel,vR).x,T=texture(vel,vT).y,B=texture(vel,vB).y;" +
      "if(vL.x<0.)L=-C.x;if(vR.x>1.)R=-C.x;if(vT.y>1.)T=-C.y;if(vB.y<0.)B=-C.y;o=vec4(.5*(R-L+T-B),0.,0.,1.);}",
    clear: "uniform sampler2D src;uniform float k;void main(){o=k*texture(src,vUv);}",
    pressure: "uniform sampler2D pr,divT;void main(){float L=texture(pr,vL).x,R=texture(pr,vR).x,T=texture(pr,vT).x,B=texture(pr,vB).x;" +
      "o=vec4((L+R+B+T-texture(divT,vUv).x)*.25,0.,0.,1.);}",
    grad: "uniform sampler2D pr,vel;void main(){float L=texture(pr,vL).x,R=texture(pr,vR).x,T=texture(pr,vT).x,B=texture(pr,vB).x;" +
      "o=vec4(texture(vel,vUv).xy-vec2(R-L,T-B),0.,1.);}",
    advect: "uniform sampler2D vel,src;uniform vec2 simTexel;uniform float dt,diss;" +
      "void main(){vec2 c=vUv-dt*texture(vel,vUv).xy*simTexel;o=texture(src,c)/(1.+diss*dt);}",
    // Backward map: content at x came from x-off. Advected with the fluid, relaxes to identity.
    offset: "uniform sampler2D vel,src;uniform vec2 simTexel;uniform float dt,heal,disp;" +
      "void main(){vec2 step=dt*texture(vel,vUv).xy*simTexel;vec2 off=texture(src,vUv-step).xy+step*disp;o=vec4(off*exp(-heal*dt),0.,1.);}",
    show: "uniform sampler2D dye,wordT;uniform highp sampler2DArray balloons;uniform vec4 wordRect,balloonRect;" +
      "uniform ivec4 slots;uniform vec4 tint[3];uniform vec2 cursor,dyeTexel;uniform vec3 paper,letterC;" +
      "uniform float aspect,threshold,refract,slope,time;" +
      "vec2 inRect(vec2 p,vec4 r){return (p-r.xy)/r.zw;}" +
      "float inside(vec2 t){return step(0.,t.x)*step(t.x,1.)*step(0.,t.y)*step(t.y,1.);}" +
      "float thick(vec2 p){vec4 d=texture(dye,p);return d.r+d.g+d.b+d.a;}" +
      "float height(float t){return sqrt(clamp((t-threshold)/1.2,0.,1.));}" +
      // Studio softbox seen in reflection: soft rectangle around direction c.
      "float box(vec3 r,vec3 c,vec2 hs){vec3 C=normalize(c),R=normalize(cross(vec3(0.,1.,0.),C)),U=cross(C,R);float f=dot(r,C);" +
      "if(f<=0.)return 0.;vec2 p=abs(vec2(dot(r,R),dot(r,U))/f);vec2 q=1.-smoothstep(hs*.7,hs,p);return q.x*q.y;}" +
      "void main(){vec2 uv=vUv;float T=thick(uv);if(T<threshold*.5){o=vec4(0.);return;}" +
      "float cov=smoothstep(threshold-.012,threshold+.012,T),h=height(T);" +
      // Surface normal from the thickness gradient: a meniscus at the edge, a dome inside.
      "vec2 e=dyeTexel*2.5;float hx=height(thick(uv+vec2(e.x,0.)))-height(thick(uv-vec2(e.x,0.)));" +
      "float hy=height(thick(uv+vec2(0.,e.y)))-height(thick(uv-vec2(0.,e.y)));" +
      "vec3 n=normalize(vec3(-hx*slope,-hy*slope,1.));" +
      // Under the lens the logo swells into its rendered volume; the image bends at the slopes.
      "vec2 ruv=uv+n.xy*refract*(.35+h);vec2 tb=inRect(ruv,balloonRect);vec4 bal=texture(balloons,vec3(tb,0.))*inside(tb);" +
      "vec3 under=mix(paper,bal.rgb,bal.a);" +
      // Lacquers that meet blend by amount, like real ones.
      "vec4 d=texture(dye,uv);vec4 tn=(d.r*tint[slots.x]+d.g*tint[slots.y]+d.b*tint[slots.z]+d.a*tint[slots.w])/max(T,1e-4);" +
      "vec3 liquid=under*pow(tn.rgb,vec3(.35+1.0*h));liquid=mix(liquid,vec3(.975,.975,.965),tn.a*(.25+.4*h));" +
      "vec3 r=reflect(vec3(0.,0.,-1.),n);float fr=.04+.96*pow(1.-max(n.z,0.),5.);" +
      "float sp=box(r,vec3(-.45,.55,.7),vec2(.5,.26))*1.0+box(r,vec3(.8,.1,.6),vec2(.07,.9))*.8+box(r,vec3(0.,1.,.15),vec2(.9,.5))*.15;" +
      "vec3 L=normalize(vec3((cursor-uv)*vec2(aspect,1.),.16)),Hv=normalize(L+vec3(0.,0.,1.));" +
      "float gl=pow(max(dot(n,Hv),0.),2400.)*.6;" +
      "liquid+=vec3(1.)*(sp*mix(.18,1.,fr)+gl);" +
      "liquid*=1.-.22*cov*(1.-smoothstep(0.,.22,h));" +                        // meniscus line
      // Outside the lacquer: flat word on paper, a faint coloured shadow and a caustic rim.
      "vec2 tw=inRect(uv,wordRect);vec3 col=mix(paper,letterC,texture(wordT,tw).a*inside(tw));" +
      "float sh=smoothstep(threshold*.6,threshold,thick(uv+vec2(-.004,.012)));" +
      "col*=1.-.07*sh*(1.-cov);col=mix(col,col*tn.rgb,.35*sh*(1.-cov));" +
      "float ca=smoothstep(threshold*.55,threshold*.97,T)*(1.-cov);col+=mix(vec3(1.),tn.rgb,.6)*ca*.10;" +
      "float cover=smoothstep(threshold*.5,threshold*.8,T);o=vec4(mix(col,liquid,cov)*cover,cover);}"
  };

  var programs, quad, fbo, simW, simH, dyeW, dyeH, W = 0, H = 0, aspect = 1;
  var wordTex, balloonTex, dataTex, ready = false, wordReady = false, tier = null, epoch = 0;
  var wordRect = [0, 0, 1, 1], balloonRect = [0, 0, 1, 1];
  var running = false, raf = 0, last = 0, lastInput = -1e9, visible = true, manual = false;
  var pointer = null, slots = [0, 0, 0, 0], slotAt = [0, 0, 0, 0], slot = 0, nextMaterial = 0, strokeEnd = -1e9;
  var drops = [], splats = [], cursorT = [0.5, 0.6], cursorS = [0.5, 0.6], dirty = null, boxes = [], clock = 0;
  var paper = config.paper || [241,241,235], letterC = config.letterColor || [30,30,30], inkC = config.inkColor || [5,5,5];

  function compile(type, src) {
    var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  function program(fs) {
    var p = gl.createProgram();
    gl.attachShader(p, compile(gl.VERTEX_SHADER, VS)); gl.attachShader(p, compile(gl.FRAGMENT_SHADER, HEAD + fs));
    gl.bindAttribLocation(p, 0, "aPos"); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    var u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < n; i++) { var info = gl.getActiveUniform(p, i); u[info.name] = gl.getUniformLocation(p, info.name); }
    return { p: p, u: u };
  }
  function params(kind) {
    gl.texParameteri(kind, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(kind, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(kind, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(kind, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }
  function target(w, h, internal, format) {
    var t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t); params(gl.TEXTURE_2D);
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0, format, gl.HALF_FLOAT, null);
    var f = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, f);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
    gl.viewport(0, 0, w, h); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    return { t: t, f: f, w: w, h: h };
  }
  function pair(w, h, internal, format) {
    var a = target(w, h, internal, format), b = target(w, h, internal, format);
    return { read: a, write: b, swap: function () { var x = this.read; this.read = this.write; this.write = x; } };
  }
  function bind(unit, tex, kind) { gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(kind || gl.TEXTURE_2D, tex); return unit; }
  var draws=0;
  function draw(prog, dest, set) {
    draws++;
    gl.useProgram(prog.p);
    var w = dest ? dest.w : canvas.width, h = dest ? dest.h : canvas.height;
    if (prog.u.texel) gl.uniform2f(prog.u.texel, 1 / w, 1 / h);
    set(prog.u);
    gl.bindFramebuffer(gl.FRAMEBUFFER, dest ? dest.f : null);
    gl.viewport(0, 0, w, h);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // Context, programs and textures; called again after a lost GPU context is restored.
  function setup() {
    if (reduce.matches) return;
    gl = gl || canvas.getContext("webgl2", { alpha: true, antialias: false, depth: false, stencil: false, premultipliedAlpha: true });
    if (!gl || gl.isContextLost() || !(gl.getExtension("EXT_color_buffer_float") || gl.getExtension("EXT_color_buffer_half_float"))) return;
    try {
      programs = {};
      for (var k in FS) programs[k] = program(FS[k]);
    } catch (e) { console.warn("hero ink:", e); gl = null; return; }
    quad = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    wordTex = gl.createTexture(); balloonTex = gl.createTexture(); dataTex = gl.createTexture();
    epoch++; ready = wordReady = false; tier = null; W = H = 0; fbo = {};
    resize();
  }

  function resize() {
    if (!programs || !gl) return;
    var r = hero.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (r.width < 1 || r.height < 1) return;
    var w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
    aspect = r.width / r.height;
    if (w !== W || h !== H) {
      stop();
      if(fbo)Object.values(fbo).forEach(function(v){(v.read?[v.read,v.write]:[v]).forEach(function(t){gl.deleteTexture(t.t);gl.deleteFramebuffer(t.f);});});
      W = canvas.width = w; H = canvas.height = h;
      var small = Math.min(r.width, r.height) < 600;
      var sim = small ? 128 : P.sim, dye = small ? 640 : P.dye;
      simW = aspect >= 1 ? Math.round(sim * aspect) : sim; simH = aspect >= 1 ? sim : Math.round(sim / aspect);
      dyeW = aspect >= 1 ? dye : Math.round(dye * aspect); dyeH = aspect >= 1 ? Math.round(dye / aspect) : dye;
      fbo.vel = pair(simW, simH, gl.RG16F, gl.RG); fbo.off = pair(simW, simH, gl.RG16F, gl.RG);
      fbo.pr = pair(simW, simH, gl.R16F, gl.RED); fbo.div = target(simW, simH, gl.R16F, gl.RED);
      fbo.curl = target(simW, simH, gl.R16F, gl.RED); fbo.dye = pair(dyeW, dyeH, gl.RGBA16F, gl.RGBA);
    }
    // Exact word rect (outline without the SVG's transparent pad), in canvas uv (y up).
    var b = word.getBoundingClientRect(), sx = b.width / (SVG.w + 2 * SVG.pad), sy = b.height / (SVG.h + 2 * SVG.pad);
    var x = b.left - r.left + SVG.pad * sx, y = b.top - r.top + SVG.pad * sy, ww = SVG.w * sx, wh = SVG.h * sy;
    wordRect = [x / r.width, 1 - (y + wh) / r.height, ww / r.width, wh / r.height];
    balloonRect = [(x - BALLOON.mx * wh) / r.width, 1 - (y + wh + BALLOON.my * wh) / r.height,
      (ww + 2 * BALLOON.mx * wh) / r.width, (wh + 2 * BALLOON.my * wh) / r.height];
    P.capUv = wh / r.height; P.minRadius = 24 / r.height;
    rasterWord(b.width * dpr, b.height * dpr);
    loadBalloons((ww + 2 * BALLOON.mx * wh) * dpr);
  }

  function rasterWord(w, h) {
    var img = new Image(), mine = epoch;
    img.onload = function () {
      if (mine !== epoch || !gl) return;
      var sx = w / (SVG.w + 2 * SVG.pad), sy = h / (SVG.h + 2 * SVG.pad);
      var c = document.createElement("canvas"); c.width = Math.max(1, Math.round(SVG.w * sx)); c.height = Math.max(1, Math.round(SVG.h * sy));
      c.getContext("2d").drawImage(img, -SVG.pad * sx, -SVG.pad * sy, w, h);
      gl.bindTexture(gl.TEXTURE_2D, wordTex);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, c); params(gl.TEXTURE_2D);
      wordReady = true;
    };
    img.src = word.currentSrc || word.src;
  }

  // Smallest set that covers the balloon frame at device pixels; AVIF where the browser decodes it.
  var loaded = [];
  function whenIdle(fn) {
    var go=function(){ (window.requestIdleCallback || function(f){setTimeout(f,100);})(fn,{timeout:1500}); };
    if(document.readyState==='complete')go();else addEventListener('load',go,{once:true});
  }
  function loadBalloons(needPx) {
    var t=manifest.tiers.find(function(x){return x.width>=needPx;}) || manifest.tiers[manifest.tiers.length-1];
    if(tier && tier.width>=t.width)return;
    tier=t; var mine=++epoch; ready=false; loaded=[];
    // resize() starts rasterWord before this; share the same generation.
    rasterWord(word.clientWidth*Math.min(devicePixelRatio,2),word.clientHeight*Math.min(devicePixelRatio,2));
    function fetchImg(src){return new Promise(function(resolve,reject){var im=new Image(); im.decoding='async'; im.onload=function(){resolve(im);}; im.onerror=reject; im.src=assetURL(src);});}
    function layer(tex,img,i,n,first){
      gl.bindTexture(gl.TEXTURE_2D_ARRAY,tex);gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,true);
      if(first){gl.texImage3D(gl.TEXTURE_2D_ARRAY,0,gl.RGBA8,img.width,img.height,n,0,gl.RGBA,gl.UNSIGNED_BYTE,null);params(gl.TEXTURE_2D_ARRAY);}
      gl.texSubImage3D(gl.TEXTURE_2D_ARRAY,0,0,0,i,img.width,img.height,1,gl.RGBA,gl.UNSIGNED_BYTE,img);
    }
    whenIdle(async function(){
      try {
        var name=config.material || manifest.materials[0];
        var im=await fetchImg(t.colors[name].webp);if(mine!==epoch || !gl || gl.isContextLost())return;
        layer(balloonTex,im,0,1,true);ready=true;
      }catch(e){console.warn('Studio assets unavailable; SVG retained',e);}
    });
  }
  function splat(dest, a, b, radius, value, cap) {
    draw(programs.splat, dest.write, function (u) {
      gl.uniform1i(u.src, bind(0, dest.read.t)); gl.uniform2f(u.a, a[0], a[1]); gl.uniform2f(u.b, b[0], b[1]);
      gl.uniform1f(u.aspect, aspect); gl.uniform1f(u.radius, radius); gl.uniform4fv(u.value, value); gl.uniform1f(u.cap, cap || 0);
    });
    dest.swap();
  }

  function step(dt) {
    var simTexel = [1 / simW, 1 / simH];
    splats.forEach(function (s) {
      var r = Math.max(P.radius * P.capUv, P.minRadius); r = r * r * s.size;
      // Area the ink can reach: splat extent plus room for the flow to carry it.
      var ey = 2.2 * Math.sqrt(r) + 0.14, ex = ey / aspect + 0.04;
      boxes.push({ t: clock, b: [Math.min(s.a[0], s.b[0]) - ex, Math.min(s.a[1], s.b[1]) - ey, Math.max(s.a[0], s.b[0]) + ex, Math.max(s.a[1], s.b[1]) + ey] });
      splat(fbo.vel, s.a, s.b, r * 0.8, [s.vx * P.force * simW, s.vy * P.force * simH, 0, 0]);
      var v = [0, 0, 0, 0]; v[s.slot] = s.ink; splat(fbo.dye, s.a, s.b, r, v, 2.2);
    });
    splats.length = 0;
    clock += dt; boxes = boxes.filter(function (x) { return clock - x.t < 3.5; });
    dirty = boxes.reduce(function (d, x) { var q = x.b; return d ? [Math.min(d[0], q[0]), Math.min(d[1], q[1]), Math.max(d[2], q[2]), Math.max(d[3], q[3])] : q.slice(); }, null);
    draw(programs.curl, fbo.curl, function (u) { gl.uniform1i(u.vel, bind(0, fbo.vel.read.t)); });
    draw(programs.vort, fbo.vel.write, function (u) {
      gl.uniform1i(u.vel, bind(0, fbo.vel.read.t)); gl.uniform1i(u.curlT, bind(1, fbo.curl.t)); gl.uniform1i(u.dye, bind(2, fbo.dye.read.t));
      gl.uniform1f(u.curl, P.curl); gl.uniform1f(u.dt, dt); gl.uniform1f(u.gravity, P.gravity);
    }); fbo.vel.swap();
    draw(programs.div, fbo.div, function (u) { gl.uniform1i(u.vel, bind(0, fbo.vel.read.t)); });
    draw(programs.clear, fbo.pr.write, function (u) { gl.uniform1i(u.src, bind(0, fbo.pr.read.t)); gl.uniform1f(u.k, 0.8); }); fbo.pr.swap();
    for (var i = 0; i < P.pressureIters; i++) {
      draw(programs.pressure, fbo.pr.write, function (u) { gl.uniform1i(u.pr, bind(0, fbo.pr.read.t)); gl.uniform1i(u.divT, bind(1, fbo.div.t)); });
      fbo.pr.swap();
    }
    draw(programs.grad, fbo.vel.write, function (u) { gl.uniform1i(u.pr, bind(0, fbo.pr.read.t)); gl.uniform1i(u.vel, bind(1, fbo.vel.read.t)); }); fbo.vel.swap();
    draw(programs.advect, fbo.vel.write, function (u) {
      gl.uniform1i(u.vel, bind(0, fbo.vel.read.t)); gl.uniform1i(u.src, bind(1, fbo.vel.read.t));
      gl.uniform2fv(u.simTexel, simTexel); gl.uniform1f(u.dt, dt); gl.uniform1f(u.diss, P.velDiss);
    }); fbo.vel.swap();
    draw(programs.advect, fbo.dye.write, function (u) {
      gl.uniform1i(u.vel, bind(0, fbo.vel.read.t)); gl.uniform1i(u.src, bind(1, fbo.dye.read.t));
      gl.uniform2fv(u.simTexel, simTexel); gl.uniform1f(u.dt, dt); gl.uniform1f(u.diss, P.dyeDiss);
    }); fbo.dye.swap();
  }

  function show(time) {
    gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    if (!dirty) return;
    var x0 = Math.max(0, Math.floor(dirty[0] * canvas.width)), y0 = Math.max(0, Math.floor(dirty[1] * canvas.height));
    var x1 = Math.min(canvas.width, Math.ceil(dirty[2] * canvas.width)), y1 = Math.min(canvas.height, Math.ceil(dirty[3] * canvas.height));
    if (x1 <= x0 || y1 <= y0) return;
    gl.enable(gl.SCISSOR_TEST); gl.scissor(x0, y0, x1 - x0, y1 - y0);
    hero.studioArea = ((x1 - x0) * (y1 - y0)) / (canvas.width * canvas.height);

    draw(programs.show, null, function (u) {
      gl.uniform1i(u.dye, bind(0, fbo.dye.read.t)); gl.uniform1i(u.wordT, bind(2, wordTex));
      gl.uniform1i(u.balloons, bind(3, balloonTex, gl.TEXTURE_2D_ARRAY));
      gl.uniform4fv(u["tint[0]"], [].concat.apply([], MATERIALS.map(function (n) { return TINT[n]; })));
      cursorS[0] += (cursorT[0] - cursorS[0]) * 0.12; cursorS[1] += (cursorT[1] - cursorS[1]) * 0.12;
      gl.uniform2f(u.cursor, cursorS[0], cursorS[1]); gl.uniform2f(u.dyeTexel, 1 / dyeW, 1 / dyeH);
      gl.uniform4fv(u.wordRect, wordRect); gl.uniform4fv(u.balloonRect, balloonRect);
      gl.uniform4i(u.slots, slots[0], slots[1], slots[2], slots[3]);
      gl.uniform3f(u.paper, paper[0] / 255, paper[1] / 255, paper[2] / 255);
      gl.uniform3f(u.letterC, letterC[0] / 255, letterC[1] / 255, letterC[2] / 255);
      gl.uniform1f(u.aspect, aspect); gl.uniform1f(u.threshold, P.threshold);
      gl.uniform1f(u.refract, P.refract); gl.uniform1f(u.slope, P.slope); gl.uniform1f(u.time, time);
    });
    gl.disable(gl.SCISSOR_TEST);
  }

  function dripsStep(dt) {
    for (var i = drops.length - 1; i >= 0; i--) {
      var d = drops[i];
      d.vy += 0.5 * dt; var py = d.y; d.y -= d.vy * dt; d.life -= dt;
      splats.push({ a: [d.x, py], b: [d.x, d.y], vx: 0, vy: 0, size: d.size, ink: d.ink, slot: d.slot });
      d.size *= 0.992;
      if (d.y < -0.05 || d.life <= 0) drops.splice(i, 1);
    }
  }

  // Ink lives ~1.5 s after the last input; then the canvas hides and the SVG shows again.
  function stop() {
    running = false; if (raf) cancelAnimationFrame(raf); raf = 0;
    hero.classList.remove("hero-live"); drops.length = splats.length = 0; dirty=null; boxes=[];
    if (fbo && fbo.dye) [fbo.dye, fbo.vel, fbo.off, fbo.pr].forEach(function (x) {
      [x.read, x.write].forEach(function (t) { gl.bindFramebuffer(gl.FRAMEBUFFER, t.f); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT); });
    });
  }
  function frame(now) {
    raf = 0;
    if (!gl || gl.isContextLost()) return;
    if (!visible || document.hidden) return;              // paused; wake() resumes
    var dt = Math.min((now - last) / 1000, 1 / 30); last = now;
    dripsStep(dt); step(dt); show(now / 1000);
    if (now - lastInput < 4200 || drops.length) raf = requestAnimationFrame(frame);
    else stop();
  }
  function wake() {
    if (!ready || !wordReady || reduce.matches || !gl || gl.isContextLost()) return false;
    if (!running) { running = true; last = performance.now(); hero.classList.add("hero-live"); }
    if (!raf && !manual && visible && !document.hidden) { last = performance.now(); raf = requestAnimationFrame(frame); }
    return true;
  }

  function newStroke() {
    // Reuse the slot that has rested longest, give it the next material.
    var oldest = 0;
    for (var i = 1; i < 4; i++) if (slotAt[i] < slotAt[oldest]) oldest = i;
    slot = oldest; slots[slot] = nextMaterial; nextMaterial = (nextMaterial + 1) % MATERIALS.length;
  }

  function move(clientX, clientY, now, fresh) {
    if (!wake()) return;
    var r = hero.getBoundingClientRect();
    var p = [(clientX - r.left) / r.width, 1 - (clientY - r.top) / r.height];
    lastInput = manual ? now : performance.now();   // same clock as rAF; event timeStamps may lag
    if (fresh || !pointer || now - strokeEnd > P.stroke) { newStroke(); pointer = null; }
    if (pointer) {
      var dx = p[0] - pointer.p[0], dy = p[1] - pointer.p[1], dist = Math.hypot(dx * aspect, dy);
      if (dist > 0.0005) {
        // Speed-based force, scaled per event so 60/120 Hz pointers push as hard as 30 Hz ones.
        var span = Math.max(now - pointer.t, 8) / 1000, k = Math.min(1, P.maxSpeed / (dist / span)) * Math.min(1, span * 30);
        splats.push({ a: pointer.p, b: p, vx: dx / span * k, vy: dy / span * k, size: 1, ink: P.ink, slot: slot });
        // Heavy strokes shed drips below the trail.
        if (dist > 0.012 && Math.random() < Math.min(0.10, dist * 2)) {
          var big = Math.random() < 0.35;
          drops.push({ x: p[0] + (Math.random() - 0.5) * 0.05, y: p[1] - P.capUv * (0.3 + Math.random() * 0.3), vy: 0.01 + Math.random() * 0.03,
            size: big ? 0.06 + Math.random() * 0.04 : 0.03 + Math.random() * 0.02, ink: 1.1, slot: slot, life: 0.8 + Math.random() * 1.2 });
        }
        slotAt[slot] = now; strokeEnd = now;
      }
    } else {
      splats.push({ a: p, b: p, vx: 0, vy: 0, size: 0.45, ink: P.ink, slot: slot });
      slotAt[slot] = now; strokeEnd = now;
    }
    pointer = { p: p, t: now }; cursorT = p;
  }

  setup();
  new ResizeObserver(function () { resize(); }).observe(hero);
  new IntersectionObserver(function (es) { visible = es[0].isIntersecting; if (visible && running) wake(); }).observe(hero);
  document.addEventListener("visibilitychange", function () { if (!document.hidden && running) wake(); });
  reduce.addEventListener("change", function () { if (reduce.matches) stop(); else if (!programs) setup(); });
  hero.addEventListener("pointermove", function (e) { if (e.pointerType === "mouse" || e.pointerType === "pen") move(e.clientX, e.clientY, e.timeStamp, false); }, { passive: true });
  hero.addEventListener("pointerleave", function () { pointer = null; }, { passive: true });
  hero.addEventListener("touchstart", function (e) { var t = e.touches[0]; move(t.clientX, t.clientY, e.timeStamp, true); }, { passive: true });
  hero.addEventListener("touchmove", function (e) { var t = e.touches[0]; move(t.clientX, t.clientY, e.timeStamp, false); }, { passive: true });
  hero.addEventListener("touchend", function () { pointer = null; }, { passive: true });
  canvas.addEventListener("webglcontextlost", function (e) { e.preventDefault(); stop(); epoch++; ready = wordReady = false; programs = null; });
  canvas.addEventListener("webglcontextrestored", function () { setup(); });

  // Deterministic stepping for acceptance checks and frame-by-frame comparison.
  var simNow = 0;
  hero.studio = {
    stats: function () { return {draws:draws,running:running,ready:ready && wordReady}; },
    ready: function () { return ready && wordReady; },
    manual: function () { manual = true; if (raf) cancelAnimationFrame(raf); raf = 0; },
    tick: function (dt, x, y) {
      simNow += dt * 1000;
      if (x != null) move(x, y, simNow, false); else pointer = null;
      dripsStep(dt); step(dt); show(simNow / 1000);
    }
  };
}
