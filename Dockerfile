FROM shrinand/k8s-minion-manager:v0.1

COPY . /
ENV PYTHONPATH=/
RUN mv binaries/kubectl-v1.12.3-linux-amd64 /usr/local/bin/kubectl
RUN chmod u+x minion_manager.py
