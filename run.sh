JSON_DIR=../openpose/json_output
python src/openpose_3dpose_sandbox.py --camera_frame --residual --batch_norm --dropout 0.5 --max_norm --evaluateActionWise --use_sh --epochs 200 --load 4874200 --openpose $JSON_DIR --write_gif --gif_fps 30 --verbose 3 --text_output
