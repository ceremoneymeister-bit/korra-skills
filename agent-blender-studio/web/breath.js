/* MIT · Korra. Blend actual cloth snapshots independently for each SVG figure. */
export function mount(config,m,assetURL) {
  const hero=document.querySelector(config.selector),canvas=hero.querySelector('.hero-canvas'),word=hero.querySelector('.hero-fallback');
  const reduce=matchMedia('(prefers-reduced-motion: reduce)'),N=m.shapes.length;
  if(!m.stages)return;
  let gl,program,atlas,wordTex,u={},ready=false,visible=true,raf=0,draws=0,last=0,generation=0;
  const levels=new Float32Array(N), touched=new Float64Array(N);
  touched.fill(-1e9);
  const vertex='#version 300 es\nin vec2 p;out vec2 uv;void main(){uv=p*.5+.5;gl_Position=vec4(p,0.,1.);}';
  const fragment=`#version 300 es
precision highp float;precision highp sampler2DArray;
in vec2 uv;out vec4 color;
uniform sampler2D wordT;uniform sampler2DArray atlas;
uniform vec4 wr,fr,crop[${N}],ar[${N}],shape[${N}];uniform float level[${N}];uniform int stages;uniform vec3 paper;
float inside(vec2 p){return step(0.,p.x)*step(p.x,1.)*step(0.,p.y)*step(p.y,1.);}
void main(){
vec2 w=(uv-wr.xy)/wr.zw;vec4 flatColor=texture(wordT,w)*inside(w);float erase=0.;
for(int i=0;i<${N};i++){vec2 q=vec2(w.x,1.-w.y);if(q.x>=shape[i].x&&q.x<=shape[i].z&&q.y>=shape[i].y&&q.y<=shape[i].w)erase=max(erase,smoothstep(0.,.1,level[i]));}
vec3 col=mix(paper,flatColor.rgb,flatColor.a*(1.-erase));
vec2 fp=vec2((uv.x-fr.x)/fr.z,1.-(uv.y-fr.y)/fr.w);
for(int i=0;i<${N};i++){
if(level[i]<.001)continue;vec2 t=(fp-crop[i].xy)/(crop[i].zw-crop[i].xy);if(inside(t)<.5)continue;
vec2 a=mix(ar[i].xy,ar[i].zw,t);a.y=1.-a.y;
float s=level[i]*float(stages-1);vec4 b=mix(texture(atlas,vec3(a,floor(s))),texture(atlas,vec3(a,min(ceil(s),float(stages-1)))),fract(s));
col=mix(col,b.rgb,b.a*smoothstep(0.,.1,level[i]));}
color=vec4(col,1.);}`;
  const load=src=>new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=reject;im.src=assetURL(src);});
  function stop(){cancelAnimationFrame(raf);raf=0;levels.fill(0);touched.fill(-1e9);hero.classList.remove('hero-live');}
  function shader(type,src){const s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s;}
  function params(type){gl.texParameteri(type,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(type,gl.TEXTURE_MAG_FILTER,gl.LINEAR);gl.texParameteri(type,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(type,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);}
  async function setup(){
    if(reduce.matches)return;
    const mine=++generation;ready=false;
    try{
      gl=canvas.getContext('webgl2',{alpha:true,antialias:false,depth:false});if(!gl)return;
      if(N*14>gl.getParameter(gl.MAX_FRAGMENT_UNIFORM_VECTORS)*4)throw Error('Too many shapes for this GPU');
      program=gl.createProgram();gl.attachShader(program,shader(gl.VERTEX_SHADER,vertex));gl.attachShader(program,shader(gl.FRAGMENT_SHADER,fragment));gl.bindAttribLocation(program,0,'p');gl.linkProgram(program);
      if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(program));
      const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);gl.enableVertexAttribArray(0);gl.vertexAttribPointer(0,2,gl.FLOAT,false,0,0);
      for(const name of ['wordT','atlas','wr','fr','crop','ar','shape','level','stages','paper'])u[name]=gl.getUniformLocation(program,['crop','ar','shape','level'].includes(name)?name+'[0]':name);
      const image=await load(m.svg.file);if(mine!==generation)return;
      // Rasterize vector to a texture with explicit pixel dimensions.
      const c=document.createElement('canvas');c.width=m.size[0];c.height=Math.max(1,Math.round(c.width/m.svg.w));c.getContext('2d').drawImage(image,0,0,c.width,c.height);
      wordTex=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,wordTex);gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,true);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,c);params(gl.TEXTURE_2D);
      atlas=gl.createTexture();const files=m.stages.files[config.material||m.materials[0]];
      if(files.length>gl.getParameter(gl.MAX_ARRAY_TEXTURE_LAYERS))throw Error('Too many stages for GPU');
      for(let i=0;i<files.length;i++){
        const image=await load(files[i]);if(mine!==generation||gl.isContextLost())return;
        if(Math.max(image.width,image.height)>gl.getParameter(gl.MAX_TEXTURE_SIZE))throw Error('Atlas too large for GPU');
        gl.bindTexture(gl.TEXTURE_2D_ARRAY,atlas);
        if(i===0){gl.texImage3D(gl.TEXTURE_2D_ARRAY,0,gl.RGBA8,image.width,image.height,files.length,0,gl.RGBA,gl.UNSIGNED_BYTE,null);params(gl.TEXTURE_2D_ARRAY);}
        gl.texSubImage3D(gl.TEXTURE_2D_ARRAY,0,0,0,i,image.width,image.height,1,gl.RGBA,gl.UNSIGNED_BYTE,image);
      }
      ready=true;resize();
    }catch(e){stop();console.warn('Breath fallback:',e);}
  }
  function resize(){stop();const r=hero.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2);canvas.width=Math.round(r.width*d);canvas.height=Math.round(r.height*d);}
  function frame(now){
    raf=0;if(!ready||reduce.matches||!visible||document.hidden||gl.isContextLost())return;
    const dt=Math.min((now-last)/1000,.05);last=now;let active=false;
    for(let i=0;i<N;i++){const target=now-touched[i]<650?1:0;levels[i]+=(target-levels[i])*Math.min(1,dt*(target?8:3));if(levels[i]<.002&&target===0)levels[i]=0;if(levels[i]>0)active=true;}
    if(!active){stop();return;}
    const r=hero.getBoundingClientRect(),b=word.getBoundingClientRect(),mx=m.margins[0],my=m.margins[1];
    const wr=[(b.left-r.left)/r.width,1-(b.bottom-r.top)/r.height,b.width/r.width,b.height/r.height];
    const fr=[wr[0]-mx*b.height/r.width,wr[1]-my*b.height/r.height,wr[2]+2*mx*b.height/r.width,wr[3]+2*my*b.height/r.height];
    gl.viewport(0,0,canvas.width,canvas.height);gl.disable(gl.SCISSOR_TEST);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);
    // Only the union of active figure crops changes; outside it the SVG remains untouched.
    let box=[1,1,0,0];m.stages.crops.forEach((c,i)=>{if(levels[i])box=[Math.min(box[0],c[0]),Math.min(box[1],c[1]),Math.max(box[2],c[2]),Math.max(box[3],c[3])];});
    const x=Math.max(0,Math.floor((fr[0]+box[0]*fr[2])*canvas.width)),y=Math.max(0,Math.floor((fr[1]+(1-box[3])*fr[3])*canvas.height));
    gl.enable(gl.SCISSOR_TEST);gl.scissor(x,y,Math.max(0,Math.ceil((box[2]-box[0])*fr[2]*canvas.width)+2),Math.max(0,Math.ceil((box[3]-box[1])*fr[3]*canvas.height)+2));
    gl.useProgram(program);gl.activeTexture(gl.TEXTURE0);gl.bindTexture(gl.TEXTURE_2D,wordTex);gl.uniform1i(u.wordT,0);gl.activeTexture(gl.TEXTURE1);gl.bindTexture(gl.TEXTURE_2D_ARRAY,atlas);gl.uniform1i(u.atlas,1);
    gl.uniform4fv(u.wr,wr);gl.uniform4fv(u.fr,fr);gl.uniform4fv(u.crop,m.stages.crops.flat());gl.uniform4fv(u.ar,m.stages.atlas.flat());gl.uniform4fv(u.shape,m.shapes.flatMap(s=>s.rect));gl.uniform1fv(u.level,levels);gl.uniform1i(u.stages,m.stages.count);gl.uniform3fv(u.paper,(config.paper||[241,241,235]).map(v=>v/255));
    gl.drawArrays(gl.TRIANGLES,0,3);draws++;gl.disable(gl.SCISSOR_TEST);hero.classList.add('hero-live');raf=requestAnimationFrame(frame);
  }
  function wake(){if(!raf&&ready&&!reduce.matches&&visible&&!document.hidden){last=performance.now();raf=requestAnimationFrame(frame);}}
  function touch(x,y){const r=word.getBoundingClientRect(),p=[(x-r.left)/r.width,(y-r.top)/r.height];m.shapes.forEach((s,i)=>{const b=s.rect;if(p[0]>=b[0]-.025&&p[0]<=b[2]+.025&&p[1]>=b[1]-.08&&p[1]<=b[3]+.08)touched[i]=performance.now();});wake();}
  hero.addEventListener('pointermove',e=>{if(e.pointerType!=='touch')touch(e.clientX,e.clientY);},{passive:true});
  for(const name of ['touchstart','touchmove'])hero.addEventListener(name,e=>touch(e.touches[0].clientX,e.touches[0].clientY),{passive:true});
  new ResizeObserver(resize).observe(hero);
  new IntersectionObserver(es=>{visible=es[0].isIntersecting;if(!visible)stop();}).observe(hero);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});
  reduce.addEventListener('change',()=>{stop();if(!reduce.matches&&!ready)setup();});
  canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();generation++;ready=false;stop();});canvas.addEventListener('webglcontextrestored',setup);
  hero.studio={ready:()=>ready,stats:()=>({draws,ready,running:!!raf})};
  if(document.readyState==='complete')setup();else addEventListener('load',setup,{once:true});
}
