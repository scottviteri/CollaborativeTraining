apt update && apt install -y vim nano ncurses-term tmux && ssh-keygen -t rsa -b 2048 -f ~/.ssh/id_rsa -N "" && cd /root && git clone https://github.com/scottviteri/MarkovianTraining.git && pip install scipy transformers datasets==2.14.6 torchtyping==0.1.4 && pip install peft einops apache_beam==2.51.0 matplotlib wandb && pip install -U flash-attn --no-build-isolation && pip install openai bitsandbytes scipy scikit-learn
