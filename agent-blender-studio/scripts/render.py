"""Cycles studio: shared orthographic framing, materials, stage sprites and data pass."""
import argparse
import json
import math
import shutil
import sys
from pathlib import Path
import bpy
from mathutils import Vector
p=argparse.ArgumentParser()
p.add_argument('--out',type=Path,required=True)
p.add_argument('--materials',default='graphite,gold,lime')
p.add_argument('--width',type=int,default=1200)
p.add_argument('--samples',type=int,default=48)
p.add_argument('--hdri',type=Path,help='Optional Poly Haven CC0 .hdr or .exr')
p.add_argument('--stages',action='store_true',help='Render each figure separately at every stored frame')
a=p.parse_args(sys.argv[sys.argv.index('--')+1:])
if not 64<=a.width<=8192 or not 1<=a.samples<=4096: p.error('width 64..8192; samples 1..4096')
a.out.mkdir(parents=True,exist_ok=True)
scene=bpy.context.scene
meta=json.loads(scene['studio'])
objects=[o for o in scene.objects if o.type=='MESH' and 'shape' in o]
fw,fh=meta['frame']; width=a.width; height=max(1,round(width*fh/fw))
camdata=bpy.data.cameras.new('Studio camera'); camdata.type='ORTHO'; camdata.ortho_scale=fw; camdata.sensor_fit='HORIZONTAL'
cam=bpy.data.objects.new('Studio camera',camdata); scene.collection.objects.link(cam); cam.location=(0,0,10); scene.camera=cam
world=bpy.data.worlds.new('Studio'); scene.world=world; world.use_nodes=True
nt=world.node_tree; bg=nt.nodes.get('Background'); bg.inputs['Color'].default_value=(.35,.35,.35,1); bg.inputs['Strength'].default_value=.4
if a.hdri:
    env=nt.nodes.new('ShaderNodeTexEnvironment'); env.image=bpy.data.images.load(str(a.hdri.resolve())); nt.links.new(env.outputs['Color'],bg.inputs['Color'])
def area(name,pos,power,size):
    light=bpy.data.lights.new(name,'AREA'); light.energy=power; light.shape='RECTANGLE'; light.size=size; light.size_y=size*.5
    ob=bpy.data.objects.new(name,light); scene.collection.objects.link(ob); ob.location=pos; ob.rotation_euler=(-ob.location).to_track_quat('-Z','Y').to_euler()
area('Key',(-fw*.4,3,4),500,4); area('Rim',(fw*.55,1,2),350,2); area('Fill',(0,-3,3),180,3)
r=scene.render; r.engine='CYCLES'; scene.cycles.device='CPU'; scene.cycles.samples=a.samples; scene.cycles.use_denoising=True
r.film_transparent=True; r.resolution_x=width; r.resolution_y=height; r.resolution_percentage=100
r.image_settings.file_format='PNG'; r.image_settings.color_mode='RGBA'; r.image_settings.color_depth='8'
scene.view_settings.view_transform='AgX'; scene.view_settings.look='AgX - Medium High Contrast'
LOOKS = {
    # name: (hdri, hdri strength, z-rotation deg, materials per letter, strips)
    "holo": ("studio_small_09", 0.55, 30, [dict(**{"Base Color": (0.88, 0.84, 0.97, 1), "Metallic": 1.0, "Roughness": 0.08,
             "Thin Film Thickness": 640.0, "Thin Film IOR": 1.6})], "holo"),
    "gold": ("studio_small_09", 1.1, 60, [dict(**{"Base Color": (1.0, 0.70, 0.28, 1), "Metallic": 1.0, "Roughness": 0.11})], "soft"),
    "red": ("studio_small_09", 0.9, 0, [dict(**{"Base Color": (0.55, 0.008, 0.02, 1), "Roughness": 0.28, "Coat Weight": 1.0,
            "Coat Roughness": 0.03, "Subsurface Weight": 0.15, "Subsurface Radius": (0.6, 0.1, 0.1)})], "soft"),
    "pastel": ("photo_studio_loft_hall", 1.0, 0, [
        dict(**{"Base Color": (0.93, 0.30, 0.48, 1), "Roughness": 0.38, "Sheen Weight": 0.25, "Coat Weight": 0.5, "Coat Roughness": 0.18}),
        dict(**{"Base Color": (0.20, 0.42, 0.93, 1), "Roughness": 0.38, "Sheen Weight": 0.25, "Coat Weight": 0.5, "Coat Roughness": 0.18})], "soft"),
    "graphite": ("studio_small_09", 1.25, 25, [dict(**{"Base Color": (0.028, 0.029, 0.031, 1), "Roughness": 0.34, "Coat Weight": 1.0,
                 "Coat Roughness": 0.04, "Sheen Weight": 0.15})], "soft"),
    "lime": ("studio_small_09", 1.0, 0, [dict(**{"Base Color": (0.36, 0.72, 0.01, 1), "Roughness": 0.32, "Coat Weight": 1.0,
             "Coat Roughness": 0.05})], "soft"),
    "pearl": ("photo_studio_loft_hall", 1.0, 20, [dict(**{"Base Color": (0.93, 0.93, 0.95, 1), "Metallic": 0.55, "Roughness": 0.14,
              "Thin Film Thickness": 320.0, "Thin Film IOR": 1.35})], "soft"),
}

materials=[v.strip() for v in a.materials.split(',') if v.strip()]
if not materials or any(v not in LOOKS for v in materials): p.error('Unknown material; choose '+','.join(LOOKS))
def assign(mats):
    for o in objects: o.data.materials.clear(); o.data.materials.append(mats[o['shape']%len(mats)])
def render(name,frame,shape=None):
    for o in objects: o.hide_render=o['frame']!=frame or (shape is not None and o['shape']!=shape)
    r.filepath=str((a.out/name).resolve()); bpy.ops.render.render(write_still=True)
for name in materials:
    mats=[]
    for i,params in enumerate(LOOKS[name][3]):
        m=bpy.data.materials.new(name+str(i)); m.use_nodes=True; node=m.node_tree.nodes['Principled BSDF']
        for k,v in params.items(): node.inputs[k].default_value=v
        mats.append(m)
    assign(mats); render(name+'.png',meta['frames'][-1])
    if a.stages:
        for shape in meta['shapes']:
            for k,frame in enumerate(meta['frames']): render(f'{name}-shape{shape["id"]}-stage{k}.png',frame,shape['id'])
# Linear data: R/G = normal x/y mapped to 0..1; B = height mapped across heightRange.
m=bpy.data.materials.new('Normals and height'); m.use_nodes=True; t=m.node_tree; t.nodes.clear()
geo=t.nodes.new('ShaderNodeNewGeometry'); sn=t.nodes.new('ShaderNodeSeparateXYZ'); sp=t.nodes.new('ShaderNodeSeparateXYZ')
t.links.new(geo.outputs['Normal'],sn.inputs[0]); t.links.new(geo.outputs['Position'],sp.inputs[0])
def linear(sock,mul,add):
    n=t.nodes.new('ShaderNodeMath'); n.operation='MULTIPLY_ADD'; t.links.new(sock,n.inputs[0]); n.inputs[1].default_value=mul; n.inputs[2].default_value=add; return n.outputs[0]
comb=t.nodes.new('ShaderNodeCombineColor'); z0,z1=meta['heightRange']; span=max(z1-z0,.001)
for i,sock in enumerate([linear(sn.outputs['X'],.5,.5),linear(sn.outputs['Y'],.5,.5),linear(sp.outputs['Z'],1/span,-z0/span)]): t.links.new(sock,comb.inputs[i])
em=t.nodes.new('ShaderNodeEmission'); out=t.nodes.new('ShaderNodeOutputMaterial'); t.links.new(comb.outputs[0],em.inputs['Color']); t.links.new(em.outputs[0],out.inputs['Surface'])
assign([m]); scene.view_settings.view_transform='Raw'; scene.view_settings.look='None'; scene.cycles.use_denoising=False
render('data.png',meta['frames'][-1])
meta.update({'size':[width,height],'materials':materials,'stageRenders':a.stages})
# Public manifest contains no local source paths.
meta.pop('parameters',None)
(a.out/'renders.json').write_text(json.dumps(meta,indent=2))
shutil.copyfile(Path(bpy.data.filepath).parent/'source.svg',a.out/'source.svg')
print('STUDIO_RENDER',json.dumps({'materials':materials,'size':[width,height],'stages':a.stages}))
