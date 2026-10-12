"""PNG masters to portable WebP/optional AVIF tiers and cropped stage atlases."""
import argparse
import json
import shutil
from pathlib import Path
from PIL import Image
p=argparse.ArgumentParser()
p.add_argument('--renders',type=Path,required=True)
p.add_argument('--out',type=Path,required=True)
p.add_argument('--widths',default='1600,2400,3600')
p.add_argument('--avif',action='store_true')
a=p.parse_args()
m=json.loads((a.renders/'renders.json').read_text()); a.out.mkdir(parents=True,exist_ok=True)
widths=sorted(set(min(m['size'][0],int(x)) for x in a.widths.split(',')))
if widths[0]<64: p.error('width must be at least 64')
shutil.copyfile(a.renders/'source.svg',a.out/'source.svg')
m['tiers']=[]
for w in widths:
    tier={'width':w,'colors':{},'data':f'data-{w}.webp'}
    for name in m['materials']+['data']:
        im=Image.open(a.renders/(name+'.png')).convert('RGBA')
        if im.width!=w: im=im.resize((w,round(im.height*w/im.width)),Image.Resampling.LANCZOS)
        im.save(a.out/f'{name}-{w}.webp',lossless=name=='data',quality=86,method=6)
        if name!='data':
            tier['colors'][name]={'webp':f'{name}-{w}.webp'}
            if a.avif:
                im.save(a.out/f'{name}-{w}.avif',quality=65,speed=6)
                tier['colors'][name]['avif']=f'{name}-{w}.avif'
    m['tiers'].append(tier)
if m['stageRenders']:
    count=len(m['frames']); crops=[]
    for shape in m['shapes']:
        boxes=[]
        for mat in m['materials']:
            for k in range(count):
                with Image.open(a.renders/f'{mat}-shape{shape["id"]}-stage{k}.png') as im:
                    box=im.getchannel('A').getbbox()
                    if box: boxes.append(box)
        if not boxes: raise ValueError('Empty stage renders')
        box=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
        box=[max(0,box[0]-2),max(0,box[1]-2),min(m['size'][0],box[2]+2),min(m['size'][1],box[3]+2)]
        crops.append(box)
    W=sum(b[2]-b[0]+4 for b in crops); H=max(b[3]-b[1] for b in crops)
    if max(W,H)>8192: raise ValueError('Atlas exceeds 8192px: render stages smaller or fewer shapes')
    atlas=[]; x=0
    for b in crops:
        atlas.append([x/W,0,(x+b[2]-b[0])/W,(b[3]-b[1])/H]); x+=b[2]-b[0]+4
    files={}
    for mat in m['materials']:
        files[mat]=[]
        for k in range(count):
            sheet=Image.new('RGBA',(W,H)); x=0
            for shape,b in zip(m['shapes'],crops):
                with Image.open(a.renders/f'{mat}-shape{shape["id"]}-stage{k}.png') as im: sheet.paste(im.crop(b),(x,0))
                x+=b[2]-b[0]+4
            name=f'{mat}-stage{k}.webp'; sheet.save(a.out/name,quality=88,method=6); files[mat].append(name)
    m['stages']={'count':count,'files':files,'atlas':atlas,'crops':[[b[0]/m['size'][0],b[1]/m['size'][1],b[2]/m['size'][0],b[3]/m['size'][1]] for b in crops]}
(a.out/'manifest.json').write_text(json.dumps(m,indent=2))
print('PACKED',a.out/'manifest.json')
