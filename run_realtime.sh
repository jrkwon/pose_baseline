rm png/pose_frame*.png
JSON_DIR=../openpose/json_output_live
python src/openpose_3dpose_sandbox_realtime.py --camera_frame --residual --batch_norm --dropout 0.5 --max_norm --evaluateActionWise --use_sh --epochs 200 --load 4874200 --openpose $JSON_DIR --write_gif --gif_fps 30 --verbose 3 
