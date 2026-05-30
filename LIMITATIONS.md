# LIMITATIONS.md — Known Limitations of the ReID Subsystem

This document describes the known limitations of the appearance-based
ReID approach using OSNet. Understanding these limitations is essential
for correctly interpreting analytics results and setting stakeholder
expectations.

---

## 1. Face Blur / Masking

**Issue:**
Many retail environments and privacy regulations require face blurring
or masking in video footage.

**Impact on ReID:**
OSNet was trained on unmasked retail/street footage. While the network
learns full-body appearance features (not just faces), face region
features contribute to the overall embedding. Face blurring degrades
embedding quality by approximately 5–12 % on mAP metrics (based on
published occlusion studies).

**Mitigation:**
- This system does not use dedicated face detection — body appearance
  still provides sufficient signal in most cases.
- If face blurring is mandatory, set `REID_SIMILARITY_THRESHOLD` 2–3
  points lower to compensate.
- Consider a gait-based supplement for high-mask-prevalence environments.

---

## 2. Similar Clothing

**Issue:**
Two different people wearing near-identical clothing (e.g. store
uniforms, sports team colours) can produce embeddings with cosine
similarity > 0.75, which may trigger a false cross-camera merge.

**Impact:**
False visitor merges reduce the unique visitor count. In a scenario
where 10 % of visitors wear identical uniforms, expect up to 5–8 %
undercount in unique visitors.

**Mitigation:**
- Raise `REID_SIMILARITY_THRESHOLD` to 0.87–0.90 when uniform
  wearing is prevalent.
- The VisitorRegistry's low-confidence floor (0.65) prevents the
  worst merges from happening silently — ambiguous matches stay
  separate visitors.
- Post-process: flag pairs of "visitors" with unusually high mutual
  similarity (> 0.88) for manual review.

---

## 3. Occlusion

**Issue:**
When a person passes behind another person, a shelf, or any obstacle:
- ByteTrack may lose the track ID temporarily.
- The next crop after re-emergence may be a partial-body crop (torso
  only, for example).

**Impact:**
Partial-body crops degrade embedding quality. If the track is lost and
re-acquired, the new embedding may fall below the cross-camera
similarity threshold, creating a duplicate active visitor.

**Mitigation:**
- `BYTETRACK_TRACK_BUFFER` (default 30 frames) handles short occlusions
  within the same camera without requesting a new ReID.
- `MIN_CROP_AREA` (default 1600 px²) rejects very small partial crops
  that would produce low-quality embeddings.
- The embedding update uses an exponential moving average (α = 0.3)
  so a single bad crop does not corrupt the stored embedding.

---

## 4. Group Entry / Exit

**Issue:**
When multiple people enter the camera field of view simultaneously in
a group (e.g., a family group entering through a door together), their
bounding boxes may overlap. YOLO detections of overlapping persons may
be merged (NMS suppression) or their crops may contain partial
bounding-box interference from adjacent persons.

**Impact:**
- One or more group members may not be detected in the first few frames.
- Crops near group edges may contain partial bodies of adjacent persons,
  diluting the embedding quality.
- Group member similarity may spike if they are standing close together
  with overlapping bounding boxes.

**Mitigation:**
- Lower `YOLO_IOU_THRESHOLD` to 0.35 for cameras covering entry points
  where group entry is common.
- `MIN_CROP_AREA` guards against very small partial crops.
- Future: Implement part-based masking to crop only the central vertical
  stripe of a bounding box when adjacent-person interference is detected.

---

## 5. Lighting Variation

**Issue:**
Retail stores often have mixed lighting zones:
- Bright, high-contrast product spotlights
- Dim aisles
- High-contrast door/window frames (extreme over-exposure near exits)

**Impact:**
Embedding similarity for the same person across bright and dark zones
drops by approximately 3–7 % compared to uniform-lighting scenarios.
This is the primary reason `REENTRY_SIMILARITY_THRESHOLD` is set 2
points below `REID_SIMILARITY_THRESHOLD` — re-entering persons
transition through the bright/dark doorway zone.

**Mitigation:**
- OSNet pretrained on MSMT17 (which includes outdoor + indoor scenes)
  provides better robustness than models trained only on indoor datasets.
- The embedding EMA update (α = 0.3) allows gradual adaptation as the
  person moves through different lighting zones.
- Future: Domain adaptation via camera-aware normalisation layers.

---

## 6. Single-GPU Bottleneck

**Issue:**
When multiple cameras share a single GPU, the embedding step becomes
a serialisation bottleneck. At 25 FPS per camera with 4 cameras, the
system must process up to 100 embeddings/second.

**Impact:**
OSNet-x1.0 processes ~200 crops/second on a mid-range GPU at batch
size 8. With 4 cameras at 25 FPS, the system is at ~50 % GPU
utilisation — acceptable, but leaves little headroom for peak crowd
events.

**Mitigation:**
- Use `yolov8n.pt` (nano) to reduce detection latency, leaving more
  GPU time for OSNet.
- Batch crops across cameras when multiple pipelines share an embedder.
- Upgrade to `osnet_x0_75` or `osnet_x0_5` for faster inference with
  modest accuracy reduction.

---

## 7. Re-Entry Window Limitations

**Issue:**
The `REENTRY_WINDOW_SECONDS` parameter (default 300 s) caps how long
after exit a person is eligible for re-entry detection.

**Impact:**
- Visitors who leave for more than 5 minutes and return are counted as
  new visitors. This is intentional — the longer the gap, the less
  reliable the embedding match.
- In shopping destinations where 15–20 minute exits are common (e.g.
  visiting adjacent stores), the window should be raised.
- Very long windows (> 1800 s) increase memory usage and can cause
  false re-entry matches as the exited registry grows.

**Mitigation:**
- Tune via `REENTRY_WINDOW_SECONDS` environment variable without code
  changes.
- Enable `MAX_EXITED_REGISTRY_SIZE` cap (default 5000) to bound memory.

---

## 8. Distributed Deployments

**Issue:**
The `VisitorRegistry` is an in-process Python object. It cannot be
shared natively across multiple server processes (e.g., multiple
Kubernetes pods, a camera server farm).

**Impact:**
In a multi-process deployment, each process maintains its own registry,
causing cross-process visitor inflation and failed cross-camera matching
when cameras are assigned to different processes.

**Mitigation:**
- Single-process multi-threading is fully supported and thread-safe.
- For multi-process: use the Redis publisher to emit events, and
  implement a Redis-backed registry with shared state via Redis Hash +
  Redis Sorted Set (described in FUTURE_IMPROVEMENTS.md).

---

## 9. Cold-Start Embedding Quality

**Issue:**
The first 3–5 frames of a new track may produce low-quality crops
(small bbox, partial body, motion blur from entry). Early embeddings
are less reliable.

**Impact:**
Slight increase in false NEW_VISITOR events during the first second
of a person's appearance.

**Mitigation:**
- The embedding EMA update (`alpha=0.3`) means early noisy embeddings
  are gradually replaced as better crops are observed.
- Consider a `min_track_age_frames` guard (e.g. 5 frames) before first
  registry resolve — at the cost of a slight latency increase in event
  generation.
