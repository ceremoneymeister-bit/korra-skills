// One manifest shared by all renderers. Local URLs are relative to manifest.json.
export async function start(config) {
  const hero=document.querySelector(config.selector);
  if (!hero || hero.studioStarted) return;
  hero.studioStarted=true;
  try {
    const url=new URL(config.manifest,document.baseURI);
    const response=await fetch(url); if(!response.ok)throw new Error('Manifest HTTP '+response.status);
    const manifest=await response.json();
    if(manifest.version!==1 || !manifest.shapes.length || !manifest.tiers.length)throw new Error('Invalid studio manifest');
    const assetURL=path=>new URL(path,url).href;
    const word=hero.querySelector('.hero-fallback');
    word.src=assetURL(manifest.svg.file); await word.decode();
    const mode={ink:'./ink.js',lacquer:'./lacquer.js',breath:'./breath.js'}[config.effect];
    if(!mode)throw new Error('Unknown effect');
    const module=await import(mode); module.mount(config,manifest,assetURL);
  } catch(error) { console.warn('Studio fallback:',error); hero.classList.remove('hero-live'); }
}
