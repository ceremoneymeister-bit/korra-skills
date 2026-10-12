"""Build independent closed SVG shapes; run with Blender --background --python."""
import argparse
import json
import sys
from pathlib import Path
import bpy
import bmesh
from mathutils import Vector

p = argparse.ArgumentParser()
p.add_argument('--svg', type=Path, required=True)
p.add_argument('--out', type=Path, required=True)
p.add_argument('--preset', choices=['foil', 'latex', 'extrude'], default='foil')
p.add_argument('--frames', type=int, default=44)
p.add_argument('--stages', default='', help='Snapshot frames, e.g. 1,12,24,44')
p.add_argument('--voxel', type=float, default=.018)
p.add_argument('--pressure', type=float)
p.add_argument('--depth', type=float, default=.07)
p.add_argument('--bevel', type=float, default=.025)
p.add_argument('--margin-x', type=float, default=.3)
p.add_argument('--margin-y', type=float, default=.35)
p.add_argument('--shape', type=int, help='Only this zero-based shape (left to right)')
a = p.parse_args(sys.argv[sys.argv.index('--')+1:])
if not 1 <= a.frames <= 500 or not .004 <= a.voxel <= .15 or a.depth <= 0:
    p.error('frames 1..500, voxel .004..15, depth > 0 required')
frames = sorted(set(int(x) for x in a.stages.split(',') if x) | {a.frames})
if frames[0] < 1 or frames[-1] > a.frames or min(a.margin_x,a.margin_y,a.bevel) < 0:
    p.error('Invalid stages or margins')
a.out.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.gravity = (0,0,0)
bpy.ops.import_curve.svg(filepath=str(a.svg.resolve()))
bpy.context.view_layer.update()
curves = [o for o in scene.objects if o.type == 'CURVE']
if not curves: raise ValueError('No SVG curves: convert text and strokes to closed paths first')
def bounds(obs):
    pts = [o.matrix_world @ Vector(c) for o in obs for c in o.bound_box]
    return [min(v[i] for v in pts) for i in range(3)], [max(v[i] for v in pts) for i in range(3)]
curves.sort(key=lambda o: (bounds([o])[0][0], bounds([o])[0][1], o.name))
if a.shape is not None:
    if not 0 <= a.shape < len(curves): raise ValueError('shape outside SVG')
    keep = curves[a.shape]
    for o in curves:
        if o != keep: bpy.data.objects.remove(o, do_unlink=True)
    curves = [keep]
lo,hi = bounds(curves)
height = hi[1]-lo[1]
if height <= 0: raise ValueError('Empty SVG height')
centre = Vector(((lo[0]+hi[0])/2, (lo[1]+hi[1])/2,0))
width = (hi[0]-lo[0])/height
paths, shapes = [], []
def xy(v): return f'{v.x+width/2:.8f},{.5-v.y:.8f}'
for i,o in enumerate(curves):
    for sp in o.data.splines:
        if not sp.use_cyclic_u: raise ValueError('Open paths unsupported: outline strokes before import')
        for pt in sp.bezier_points:
            for attr in ('co','handle_left','handle_right'):
                v = (o.matrix_world @ getattr(pt,attr)-centre)/height
                setattr(pt,attr,Vector((v.x,v.y,0)))
        for pt in sp.points:
            v=(o.matrix_world @ Vector(pt.co[:3])-centre)/height
            pt.co=(v.x,v.y,0,1)
    o.matrix_world.identity()
    d=[]
    for sp in o.data.splines:
        pts=sp.bezier_points
        if pts:
            d.append('M'+xy(pts[0].co))
            for k in range(len(pts)):
                curr,nxt=pts[k],pts[(k+1)%len(pts)]
                d.append('C'+xy(curr.handle_right)+' '+xy(nxt.handle_left)+' '+xy(nxt.co))
        elif sp.points:
            d.append('M'+' L'.join(xy(Vector(pt.co[:3])) for pt in sp.points))
        d.append('Z')
    paths.append('<path d="'+' '.join(d)+'"/>')
    o.data.dimensions='2D'; o.data.fill_mode='BOTH'; o.data.resolution_u=20
    o.data.extrude=a.depth
    if a.preset=='extrude': o.data.bevel_depth=a.bevel; o.data.bevel_resolution=3
    o.name=f'S{i:04d}'
    bpy.context.view_layer.update()
    low,high=bounds([o])
    shapes.append({'id':i,'rect':[(low[0]+width/2)/width,.5-high[1],(high[0]+width/2)/width,.5-low[1]]})
    bpy.ops.object.select_all(action='DESELECT'); o.select_set(True); bpy.context.view_layer.objects.active=o
    bpy.ops.object.convert(target='MESH')
    o['shape']=i
    if a.preset=='extrude':
        # Curve conversion leaves coincident cap/rim vertices; weld them into a closed solid.
        bm=bmesh.new(); bm.from_mesh(o.data)
        bmesh.ops.remove_doubles(bm,verts=list(bm.verts),dist=1e-6)
        bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces)); bm.to_mesh(o.data); bm.free()
        continue
    rm=o.modifiers.new('Voxel surface','REMESH'); rm.mode='VOXEL'; rm.voxel_size=a.voxel
    bpy.ops.object.modifier_apply(modifier=rm.name)
    if a.preset=='foil':
        # Side-wall vertices on the mid-plane are the welded contour, including holes.
        band=o.vertex_groups.new(name='Welded contour')
        ids=[v.index for v in o.data.vertices if abs(v.co.z)<a.voxel*.85]
        if not ids: raise ValueError('No seam vertices; reduce voxel size')
        band.add(ids,1.0,'REPLACE'); o['seam_vertices']=len(ids)
    cl=o.modifiers.new('Inflation','CLOTH'); s=cl.settings
    s.quality=8; s.mass=.3; s.air_damping=6
    s.tension_stiffness=s.compression_stiffness=15 if a.preset=='foil' else 6
    s.shear_stiffness=s.tension_stiffness*.5; s.bending_stiffness=.5 if a.preset=='foil' else .05
    s.use_pressure=True; s.effector_weights.gravity=0
    pressure=a.pressure if a.pressure is not None else (10 if a.preset=='foil' else 14)
    shrink=-.22 if a.preset=='foil' else -.32
    if a.preset=='foil': s.vertex_group_mass=band.name; s.pin_stiffness=1
    for f,k in ((1,0),(max(2,a.frames-6),1)):
        s.uniform_pressure_force=pressure*k; s.keyframe_insert('uniform_pressure_force',frame=f)
        s.shrink_min=shrink*k; s.keyframe_insert('shrink_min',frame=f)
    cl.collision_settings.use_collision=False
    cl.point_cache.frame_start=1; cl.point_cache.frame_end=a.frames
scene.frame_end=a.frames
snapshots=[]
for f in range(1,a.frames+1):
    scene.frame_set(f)
    dg=bpy.context.evaluated_depsgraph_get()
    # Evaluation on every frame is essential in background mode.
    for o in curves: o.evaluated_get(dg).to_mesh(); o.evaluated_get(dg).to_mesh_clear()
    if f not in frames: continue
    for i,o in enumerate(curves):
        me=bpy.data.meshes.new_from_object(o.evaluated_get(dg))
        ob=bpy.data.objects.new(f'S{i:04d}_F{f:04d}',me); scene.collection.objects.link(ob)
        ob['shape']=i; ob['frame']=f; ob['seam_vertices']=o.get('seam_vertices',0)
        if a.preset!='extrude':
            sm=ob.modifiers.new('Film smoothing','CORRECTIVE_SMOOTH'); sm.iterations=3; sm.use_only_smooth=True
            sub=ob.modifiers.new('Surface','SUBSURF'); sub.levels=sub.render_levels=1
        for poly in me.polygons: poly.use_smooth=True
        snapshots.append(ob)
for o in curves: bpy.data.objects.remove(o,do_unlink=True)
bpy.context.view_layer.update()
# Expand the camera using all evaluated snapshots, keeping the source centred.
dg=bpy.context.evaluated_depsgraph_get()
low,high=bounds([o.evaluated_get(dg) for o in snapshots])
mx=max(a.margin_x,-low[0]-width/2+.05,high[0]-width/2+.05)
my=max(a.margin_y,-low[1]-.5+.05,high[1]-.5+.05)
meta={'version':1,'svg':{'w':width,'h':1,'pad':0,'file':'source.svg'},'margins':[mx,my],
      'frame':[width+2*mx,1+2*my],'shapes':shapes,'frames':frames,'preset':a.preset,
      'heightRange':[low[2],high[2]],'parameters':vars(a).copy()}
meta['parameters']={k:str(v) if isinstance(v,Path) else v for k,v in meta['parameters'].items()}
scene['studio']=json.dumps(meta)
for o in snapshots: o.hide_render=o['frame']!=a.frames
(a.out.parent/'source.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.8f} 1" fill="#1e1e1e">'+''.join(paths)+'</svg>')
(a.out.parent/'geometry.json').write_text(json.dumps(meta,indent=2))
bpy.ops.wm.save_as_mainfile(filepath=str(a.out.resolve()))
print('STUDIO_GEOMETRY',json.dumps({'shapes':len(shapes),'frames':frames,'frame':meta['frame'],'seam_vertices':[o['seam_vertices'] for o in snapshots]}))
