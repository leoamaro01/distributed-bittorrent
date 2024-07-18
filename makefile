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
	docker run --name bittorrent-client --network bittorrent-network -id bittorrent-client

run-tracker:
	docker run --name bittorrent-tracker --network bittorrent-network -p 8080:8080 -id bittorrent-tracker

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

stop-client-from-cluster:
	(make stop CONTAINER_NAME=bittorrent-client-$(INDEX) && make remove CONTAINER_NAME=bittorrent-client-$(INDEX)) || echo "Couldn't find client $(INDEX)"

stop-client-cluster:
	make stop-client-from-cluster INDEX=0
	make stop-client-from-cluster INDEX=1
	make stop-client-from-cluster INDEX=2
	make stop-client-from-cluster INDEX=3
	make stop-client-from-cluster INDEX=4

run-client-in-cluster:
	docker run --name bittorrent-client-$(INDEX) --network bittorrent-network -id bittorrent-client

run-client-cluster:
	make run-client-in-cluster INDEX=0
	make run-client-in-cluster INDEX=1
	make run-client-in-cluster INDEX=2
	make run-client-in-cluster INDEX=3
	make run-client-in-cluster INDEX=4

redeploy-client-cluster:
	make stop-client-cluster
	make build-client
	make run-client-cluster



full-redeploy: redeploy-tracker redeploy-client

.PHONY: redeploy-client-cluster run-client-cluster stop-client-from-cluster stop-client-cluster run-client-in-cluster build stop remove build-tracker build-client run-client run-tracker stop-client stop-tracker remove-client remove-tracker redeploy-tracker redeploy-client
