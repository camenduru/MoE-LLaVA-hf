
import deepspeed
import os
os.system('deepspeed --num_gpus=1 moellava/serve/gradio_web_server.py')