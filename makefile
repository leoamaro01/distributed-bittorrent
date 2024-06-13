build:
	docker build -t $(IMAGE_NAME) -f $(DOCKERFILE) .

stop:
	docker stop $(CONTAINER_NAME)

remove:
	docker rm $(CONTAINER_NAME)

build-client:
	make build IMAGE_NAME=bittorrent-client DOCKERFILE=client-dockerfile

build-tracker:
	make build IMAGE_NAME=bittorrent-tracker DOCKERFILE=tracker-dockerfile

run-client:
	docker run --name bittorrent-client --network bittorrent-network -d bittorrent-client

run-tracker:
	docker run --name bittorrent-tracker --network bittorrent-network -p 8080:8080 -d bittorrent-tracker

stop-client:
	make stop CONTAINER_NAME=bittorrent-client

stop-tracker:
	make stop CONTAINER_NAME=bittorrent-tracker

remove-client:
	make remove CONTAINER_NAME=bittorrent-client

remove-tracker:
	make remove CONTAINER_NAME=bittorrent-tracker

rerun-tracker:
	(make stop-tracker && make remove-tracker) || echo "No container found"
	make run-tracker

rerun-client:
	(make stop-client && make remove-client) || echo "No container found"
	make run-client

redeploy-tracker: 
	(make stop-tracker && make remove-tracker) || echo "No container found"
	make build-tracker
	make run-tracker

redeploy-client:
	(make stop-client && make remove-client) || echo "No container found"
	make build-client
	make run-client

full-redeploy: redeploy-tracker redeploy-client

.PHONY: build stop remove build-tracker build-client run-client run-tracker stop-client stop-tracker remove-client remove-tracker redeploy-tracker redeploy-client
