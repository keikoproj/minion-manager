FROM shrinand/k8s-minion-manager:v0.1

COPY . /
ENV PYTHONPATH=/
RUN chmod u+x minion_manager.py
