TEST_PATH=./
BUILD=docker build --target
RUN=docker run --rm -it
VERSION=v0.14-dev
GIT_TAG=$(shell git rev-parse --short HEAD)

ifeq (${DOCKER_PUSH},true)
ifndef IMAGE_NAMESPACE
$(error IMAGE_NAMESPACE must be set to push images (e.g. IMAGE_NAMESPACE=docker.mycompany.com))
endif
endif

ifdef IMAGE_NAMESPACE
IMAGE_PREFIX=${IMAGE_NAMESPACE}/
endif

ifndef IMAGE_TAG
IMAGE_TAG=${GIT_TAG}
endif

all: clean test

clean: clean-pyc clean-pytest 

clean-pyc:
		find . -name '*.pyc' -exec rm {} \;
		find . -name __pycache__ | xargs -n1 rm -rf

clean-pytest:
		rm -rf __pycache__ coverage.xml .coverage htmlcov .pytest_cache pylint.log pytest-results.xml

test: clean
		pytest --junitxml=${TEST_PATH}/pytest-results.xml -s --color=yes $(TEST_PATH)

docker-test: clean
		$(BUILD) dev -t $(IMAGE_PREFIX)minion-manager-test:$(IMAGE_TAG) .
	    $(RUN) -v ${PWD}:/src -v ~/.aws:/root/.aws $(IMAGE_PREFIX)minion-manager-test:$(IMAGE_TAG) make test

docker: clean
		$(BUILD) main -t $(IMAGE_PREFIX)minion-manager:$(IMAGE_TAG) .
		docker tag $(IMAGE_PREFIX)minion-manager:$(IMAGE_TAG) $(IMAGE_PREFIX)minion-manager:${VERSION}
		@if [ "$(DOCKER_PUSH)" = "true" ] ; then docker push $(IMAGE_PREFIX)minion-manager:$(IMAGE_TAG) ; fi

.PHONY: clean-pyc clean-pytest all test
