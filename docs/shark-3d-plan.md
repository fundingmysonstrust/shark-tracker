# Making the home-page shark realistic

Why the current shark can't get there: it is a flat SVG drawing. Realism comes from a real 3D model
(skeleton + PBR materials) lit and rendered in 3D. The plan below keeps the current scroll choreography.

## 1. Get a model (needs a human: licences and downloads)
Best value, checked 2026-11-04 (none verified by downloading; open each page and read the licence yourself):
- Quaternius "Fish Pack Animated" - CC0, has sharks with a swim loop. Stylised low-poly, so realism must come from lighting. https://quaternius.itch.io/lowpoly-animated-fish
- CGTrader "Realistic Great White Shark" (free listing, "Royalty Free No AI" licence, rigged + animated, Blender/OBJ/FBX). https://cgtrader.com/free-3d-models/animals/fish/realistic-great-white-shark-a61d0645-c771-4df4-870f-8e62db2f0438
- Paid, realistic, rigged, with swim and bite: CGTrader "Realistic Animated Great White Shark", RenderHub "Great White Shark Marine Predator - Rigged and Animated" (about $12-45).
- AVOID for a site that may become a business: Sketchfab great white models seen were CC BY-NC (non-commercial).
Needed: a rigged shark with ideally a swim clip and a bite/jaw clip, exported or convertible to .glb.

## 2. Convert and shrink
FBX/Blend -> glTF in Blender (File > Export > glTF 2.0), then:
  npx @gltf-transform/cli optimize shark.glb shark.opt.glb --compress draco --texture-compress webp --texture-size 2048
Target under about 3 MB so the page stays fast on phones. Put it in worker/src/ and serve it from the Worker.

## 3. Render it
Three.js (loaded from unpkg, which works) with: GLTFLoader + DRACOLoader, an HDRI or RoomEnvironment for reflections,
ACES tone mapping, a rim light, underwater fog, god-ray cards and caustics on the skin, a little bloom.
Skills installed for this in .claude/skills: threejs-loaders, threejs-animation, threejs-lighting, threejs-materials, threejs-asset-pipeline.

## 4. Animate
Play the model's swim clip continuously; drive the camera and the shark's position, plus a crossfade into the bite clip,
from scroll progress (same stalk / strike / thrash timeline used now, ~230vh). Add speed-based motion blur and a short camera shake on the bite.
If the model has no swim clip: bend the spine with a tail-weighted sine wave (head still, tail moving most), summing two waves.

## 5. Keep the fallback
Reduced-motion users and devices without WebGL keep the current SVG shark.
