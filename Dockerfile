FROM python:2.7.15-alpine3.8 AS Base

RUN pip install pipenv==2018.10.13
WORKDIR /src
COPY Pipfile /src/
COPY Pipfile.lock /src/
RUN wget -O /usr/local/bin/kubectl https://storage.googleapis.com/kubernetes-release/release/v1.18.16/bin/linux/amd64/kubectl
RUN chmod +x /usr/local/bin/kubectl

# This will be used as Main Image
FROM Base AS Main

RUN pipenv install --system --deploy
COPY . /src
ENV PYTHONPATH=/src
RUN chmod u+x minion_manager.py


FROM Base AS Dev

RUN apk add --no-cache build-base openssl-dev libffi-dev 
RUN pipenv install --system --deploy --dev