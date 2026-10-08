import * as THREE from 'three';
import {mountStructureMoleculeHost} from '/static/molecule-host.js';
import {projectMoleculeGeometry} from '/static/molecule-adapter.js';
const report={checks:{},scope:'Actual extracted molecule core and standalone host; synthetic decoded geometry, no classifier or production structure route proof'};
window.fixtureReport=report;
const check=(value,name)=>{if(!value)throw new Error(name);report.checks[name]=true;};
const geometry={format:'1',ref_id:'9007199254740993',version:'2',token:'fixture-physical'};
const scene={scene_token:'fixture-placed',run_id:'9007199254740995',design:{ref_id:'99',revision:'2',token:'fixture-design',source:'live-statement'},block_uid:'9',binding_token:'fixture-binding',placement_token:'fixture-placement',layer_id:'fixture-layer',role:'current',physical:geometry};
const key=(row,image)=>({geometry,row_id:row,label:row,image});
const a=key('9007199254740997',['0','0','0']),b=key('9007199254740999',['1','0','0']);
const record={scene,atoms:[{key:a,element:'C',cartesian_angstrom:[-1,0,0]},{key:b,element:'O',cartesian_angstrom:[-1,-1,0]}],bonds:[{key:{geometry,graph_token:'fixture-graph',edge_id:'9007199254741001',a,b,provenance:'stored'}}],cell:{pbc:[true,false,true],lattice_rows_angstrom:[[2,1,0],[0,3,0],[0,0,4]]},placement:{translation_host:[0,0,0],quaternion_xyzw:[0,0,0,1],angstrom_to_host:1}};
record.graph={geometry,token:'fixture-graph',source:'stored',complete:true};
const rejection=async(value,part)=>{try{await projectMoleculeGeometry(value);return false;}catch(error){return String(error).includes(part);}};
let host;
try {
  const data=await projectMoleculeGeometry(record);
  check(data.blocks[0].coords[1].join(',')==='1,0,0','nonorthogonal row-lattice absolute endpoint image');
  check(data.blocks[0].bondKeys[0].b.image[0]==='1'&&data.blocks[0].selections[0].key.row_id==='9007199254740997','exact atom bond and image keys retained');
  check(await rejection({...record,atoms:[{...record.atoms[0],key:{...a,image:['1000001','0','0']}}]},'GPU image range'),'unsupported GPU image rejected before Number conversion');
  check(await rejection({...record,atoms:[{...record.atoms[0],key:{...a,image:['0','1','0']}}]},'nonperiodic'),'partial PBC prohibits unsupported axis');
  check(await rejection({...record,scene:{...scene,role:'unadopted-artifact'}},'persisted scene'),'unadopted artifact is unavailable');
  check(await rejection({...record,cell:null},'cell unavailable'),'missing historic cell does not fall back');
  const transformed=await projectMoleculeGeometry({...record,scene:{...scene,scene_token:'fixture-transformed',placement_token:'fixture-transformed-placement'},placement:{translation_host:[2,3,0],quaternion_xyzw:[0,0,Math.SQRT1_2,Math.SQRT1_2],angstrom_to_host:2}});
  check(Math.abs(transformed.blocks[0].coords[0][0]-2)<1e-12&&Math.abs(transformed.blocks[0].coords[0][1]-1)<1e-12,'full placement rotation translation and scale');
  host=await mountStructureMoleculeHost(document.querySelector('#host'));
  check(await host.replace(data),'same extracted core mounts standalone structure host');
  const point=(x,y=0)=>{const p=new THREE.Vector3(x,y,0).project(host.camera),r=host.renderer.domElement.getBoundingClientRect();return [r.left+(p.x+1)*r.width/2,r.top+(1-p.y)*r.height/2];};
  const first=host.pick(...point(-1)),second=host.pick(...point(1));
  check(first?.key.row_id===a.row_id&&second?.key.row_id===b.row_id,'actual rendered instanced picks preserve exact source identity');
  check(first.scene.run_id===scene.run_id&&first.scene.design.token===scene.design.token&&first.scene.scene_token===scene.scene_token,'selection carries scene run and full design identity');
  for(const [name,foreign] of [
    ['foreign atom',{...first,key:{...first.key,row_id:'9007199254741997'}}],
    ['changed run',{...first,scene:{...first.scene,run_id:'9007199254741995'}}],
    ['changed geometry',{...first,key:{...first.key,geometry:{...geometry,token:'foreign-physical'}}}],
  ]) {
    const before=host.camera.position.toArray().join(',')+'|'+host.controls.target.toArray().join(',');host.highlight(foreign);
    check(!host.scene.getObjectByName('molecule-selection-marker')&&before===host.camera.position.toArray().join(',')+'|'+host.controls.target.toArray().join(','),`${name} cannot highlight or move camera`);
  }
  const edge=record.bonds[0].key;
  const graphNegatives=[
    ['bond geometry mismatch',{...record,bonds:[{key:{...edge,geometry:{...geometry,token:'foreign-physical'}}}]}],
    ['missing captured graph identity',{...record,graph:null}],
    ['graph token mismatch',{...record,bonds:[{key:{...edge,graph_token:'foreign-graph'}}]}],
    ['missing edge identity',{...record,bonds:[{key:{...edge,edge_id:null}}]}],
    ['missing edge identity',{...record,graph:{...record.graph,source:'inferred-display'},bonds:[{key:{...edge,provenance:'inferred-display',edge_id:null}}]}],
    ['graph geometry mismatch',{...record,graph:{...record.graph,geometry:{...geometry,token:'foreign-graph-geometry'}}}],
    ['bond provenance mismatch',{...record,bonds:[{key:{...edge,provenance:'inferred-display'}}]}],
    ['ambiguous edge identity',{...record,bonds:[{key:edge},{key:{...edge,a:b,b:a}}]}],
    ['captured graph incomplete or unavailable',{...record,graph:{...record.graph,complete:false}}],
    ['captured graph incomplete or unavailable',{...record,graph:{...record.graph,source:'unknown'}}],
    ['unresolved bond endpoint image',{...record,bonds:[{key:edge},{key:{...edge,edge_id:'captured-missing-second',b:{...b,image:['2','0','0']}}}]}],
    ['unresolved bond endpoint image',{...record,bonds:[{key:{...edge,b:{...b,image:['2','0','0']}}}]}],
  ];
  report.graph_negative_failures=[];
  report.graph_negative_case_count=graphNegatives.length;
  for(const [reason,input] of graphNegatives) {
    try {
      const projected=await projectMoleculeGeometry(input),block=projected.blocks[0];
      check(block.coords.length===2&&block.selections.length===2,`${reason}: atoms retained`);
      check(block.bonds.length===0&&block.bondKeys.length===0&&block.graph_availability?.status==='unavailable'&&block.graph_availability.reason===reason,`${reason}: graph explicitly unavailable and unselectable`);
    } catch(error) { report.graph_negative_failures.push({reason,error:String(error)}); }
  }
  check(report.graph_negative_failures.length===0,'all graph negatives preserve atoms and explicitly refuse graph');
  const missingEndpoint=await projectMoleculeGeometry(graphNegatives[graphNegatives.length-1][1]);await host.replace(missingEndpoint);
  check(host.pick(...point(-1))?.key.row_id===a.row_id&&host.pick(...point(1))?.key.row_id===b.row_id&&host.pick(...point(0.25))===null,'unavailable graph retains actual atom pixels/picks without bond picks');
  check(document.querySelector('[data-molecule-graph-status]').textContent.includes('unresolved bond endpoint image')&&document.querySelector('#host').dataset.moleculeGraphAvailability==='unavailable','unavailable graph reason visible in rendered host');await host.replace(data);
  const emptyGraph=await projectMoleculeGeometry({...record,bonds:[]});check(emptyGraph.blocks[0].graph_availability.status==='ready','captured empty graph remains ready');
  const transient=await projectMoleculeGeometry({...record,graph:{...record.graph,token:'fixture-inferred',source:'inferred-display'},bonds:[{key:{...edge,graph_token:'fixture-inferred',provenance:'inferred-display',edge_id:'captured-transient-edge',bond_row_id:null}}]});
  await host.replace(transient);const transientPick=host.pick(...point(0.25));check(transientPick?.key.provenance==='inferred-display'&&transientPick.key.bond_row_id===null&&transientPick.key.edge_id==='captured-transient-edge','transient captured edge stays separate with null persisted row ID');await host.replace(data);
  const bondPick=host.pick(...point(0.25));report.bond_pick=bondPick;
  check(bondPick?.key.edge_id==='9007199254741001','actual bond pick preserves edge and endpoint identities');
  host.clip([new THREE.Plane(new THREE.Vector3(1,0,0),0)]);
  check(host.pick(...point(-1))===null&&host.pick(...point(1))?.key.row_id===b.row_id,'clipped nearest hit rejected');
  const clippedPixel=new Uint8Array(4),clipGL=host.renderer.getContext(),clipRect=host.renderer.domElement.getBoundingClientRect(),clipPoint=point(-1);
  clipGL.readPixels(Math.floor(clipPoint[0]-clipRect.left),Math.floor(clipRect.height-(clipPoint[1]-clipRect.top)),1,1,clipGL.RGBA,clipGL.UNSIGNED_BYTE,clippedPixel);
  check([16,24,32].every((value,i)=>Math.abs(clippedPixel[i]-value)<3),'rendered clipping removes hidden atom pixels');
  host.clip([new THREE.Plane(new THREE.Vector3(1,0,0),0),new THREE.Plane(new THREE.Vector3(0,1,0),1)],true);
  check(host.pick(...point(-1))?.key.row_id===a.row_id,'intersection clipping keeps hit outside only one plane');host.clip([]);
  const repeated=await projectMoleculeGeometry({...record,scene:{...scene,scene_token:'fixture-repeat',run_id:'9007199254741003',design:{...scene.design,token:'same-revision-rewritten-design'},placement_token:'fixture-repeat-placement',layer_id:'repeat'},placement:{...record.placement,translation_host:[0,2,0]}});
  await host.replace({...data,blocks:[...data.blocks,...repeated.blocks]});
  const repeatedPick=host.pick(...point(-1,2));
  check(repeatedPick?.key.row_id===a.row_id&&repeatedPick.scene.scene_token==='fixture-repeat'&&repeatedPick.scene.run_id==='9007199254741003'&&repeatedPick.scene.design.token==='same-revision-rewritten-design','repeated physical geometry preserves distinct placed scene run and rewrite identity');
  await host.replace(data);
  const pixels=new Uint8Array(640*400*4),gl=host.renderer.getContext();gl.readPixels(0,0,640,400,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
  check(pixels.some((v,i)=>i%4===0&&v>100),'actual extracted atom and bond pixels');
  window.fixtureRendered=true;await new Promise(resolve=>setTimeout(resolve,1000));
  host.highlight(first);check(host.scene.getObjectByName('molecule-selection-marker').children[0].position.toArray().join(',')==='-1,0,0'&&host.controls.target.x===-1,'shared marker and reveal use actual selected position');host.highlight(null);
  host.resize(800,450);check(host.camera.aspect===800/450&&host.renderer.domElement.width===800,'standalone resize preserves camera aspect');
  let geometryDisposed=0,materialDisposed=0;
  const old=host.scene.getObjectByName('bt3d-atomic-overlay');
  const geometries=new Set(),materials=new Set();old.traverse(mesh=>{if(mesh.geometry)geometries.add(mesh.geometry);if(mesh.material)materials.add(mesh.material);});
  geometries.forEach(geo=>geo.addEventListener('dispose',()=>geometryDisposed++));materials.forEach(mat=>mat.addEventListener('dispose',()=>materialDisposed++));
  const late=host.replace(data),latest=host.replace(transformed);
  check(await late===false&&await latest===true,'replacement generation rejects late asynchronous build');
  check(geometryDisposed===geometries.size&&materialDisposed===materials.size&&!old.parent,'replacement releases every owned mesh resource');
  check(host.scene.children.filter(group=>group.name==='bt3d-atomic-overlay').length===1,'replacement leaves one molecular group');
  const beforeStaleFocus=host.camera.position.toArray().join(',');host.highlight(first);
  check(!host.scene.getObjectByName('molecule-selection-marker')&&host.camera.position.toArray().join(',')===beforeStaleFocus,'stale scene selection cannot highlight or move camera');
  const pending=host.replace(data);host.dispose();host.dispose();check(await pending===false&&!document.querySelector('#host canvas'),'idempotent disposal cancels pending build and removes canvas');
  for(let i=0;i<3;i++){const mounted=await mountStructureMoleculeHost(document.querySelector('#host'));await mounted.replace(data);mounted.dispose();}
  const lossHost=await mountStructureMoleculeHost(document.querySelector('#host'));await lossHost.replace(data);
  const extension=lossHost.renderer.getContext().getExtension('WEBGL_lose_context');check(!!extension,'actual WebGL context-loss extension available');
  extension.loseContext();await new Promise(resolve=>setTimeout(resolve,50));
  check(document.querySelector('#host').dataset.moleculeAvailability==='unavailable'&&lossHost.pick(320,200)===null,'unavailable WebGL host refuses picks');lossHost.dispose();
  check(!document.querySelector('#host canvas'),'repeated mount replace dispose leaves no canvas');
  report.pass=true;
} catch(error){report.pass=false;report.error=String(error);host?.dispose();}
document.querySelector('#result').textContent=JSON.stringify(report,null,2);window.fixtureDone=true;
