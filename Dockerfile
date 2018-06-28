FROM debian:8.5

COPY . /
RUN apt-get update && apt-get install -y \
    python \
    wget

RUN wget -P /tmp/ http://bootstrap.pypa.io/get-pip.py && \
    python /tmp/get-pip.py

RUN pip install --upgrade boto3==1.4.4 && \
    pip install --upgrade retrying==1.3.3 && \
    pip install --upgrade bunch==1.0.1 && \
    pip install --upgrade Flask==0.12 && \
    pip install --upgrade pytz==2016.10 && \
    pip install --upgrade requests==2.13.0 && \
    pip install --upgrade kubernetes==2.0.0

ENV PYTHONPATH=/
RUN chmod u+x minion_manager.py
