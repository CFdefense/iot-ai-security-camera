# Face ONNX models (YuNet + SFace)

Files are **not** committed (see `.gitignore`). They download automatically on first embedding call from:

- `face_detection_yunet_2023mar.onnx` — [opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- `face_recognition_sface_2021dec.onnx` — [opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface)

Override directory with env `FACE_MODEL_DIR` (must contain both files once fetched).
