apt update && apt install -y vim nano ncurses-term tmux && pip install transformers datasets==2.14.6 torchtyping==0.1.4 && pip install peft einops apache_beam==2.51.0 matplotlib wandb && pip install -U flash-attn --no-build-isolation && ssh-keygen -t rsa -b 2048 -f ~/.ssh/id_rsa -N "" && git clone https://github.com/scottviteri/CollaborativeTraining.git && pip install "bigbench @ https://storage.googleapis.com/public_research_data/bigbench/bigbench-0.0.1.tar.gz"
