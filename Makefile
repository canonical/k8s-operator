VM_NAME = sk8ts-vm
CONTROLLER_NAME = lxd
MODEL_NAME = sk8ts-model

.PHONY: setup shell deploy clean refresh deploy_k8s_charm redeploy_k8s_charm remove_k8s_charm create_k8s_cloud delete_k8s_cloud debug

# Setup the VM (here multipass) and install juju, lxd, charmcraft, add a model, get git repo
setup:
	multipass launch --name $(VM_NAME) -c 4 -d 40G -m 9G 22.04 
	multipass shell $(VM_NAME) -- sudo apt upgrade -y
	multipass shell $(VM_NAME) -- sudo apt update
	multipass shell $(VM_NAME) -- sudo apt install -y snapd
	multipass shell $(VM_NAME) -- sudo snap install juju --classic
	multipass shell $(VM_NAME) -- sudo lxd init --auto
	multipass shell $(VM_NAME) -- sudo adduser ubuntu lxd
	multipass shell $(VM_NAME) -- sudo -u ubuntu mkdir -p /home/ubuntu/.local/share/juju
	multipass shell $(VM_NAME) -- sudo -u ubuntu juju bootstrap localhost lxd
	multipass shell $(VM_NAME) -- juju add-model $(MODEL_NAME) --config logging-config="<root>=WARNING; unit=DEBUG"
	multipass shell $(VM_NAME) -- git clone https://github.com/canonical/k8s-operator.git
	multipass shell $(VM_NAME) -- sudo snap install charmcraft --classic

# Shell into the VM
shell:
	multipass shell $(VM_NAME)

# Deploy k8s charm and create k8s cloud and reverse
deploy: deploy_k8s_charm create_k8s_cloud

clean: delete_k8s_cloud remove_k8s_charm

refresh: clean deploy # in VM, in k8s-operator directory

# K8s charm
deploy_k8s_charm: # in VM
	charmcraft clean
	charmcraft pack
	juju deploy ./charms/worker/k8s/k8s_ubuntu-20.04-amd64_ubuntu-22.04-amd64.charm --trust

redeploy_k8s_charm: # in VM
	juju upgrade-charm k8s-worker --path=./charms/worker/k8s/k8s_ubuntu-20.04-amd64_ubuntu-22.04-amd64.charm

remove_k8s_charm: # in VM
	juju remove-application k8s-worker

# K8s cloud
create_k8s_cloud: # in VM
	juju add-k8s k8s-cloud --controller lxd --client
	juju add-model --controller lxd my-dns-model k8s-cloud
	juju bootstrap k8s-cloud

delete_k8s_cloud: # in VM
	juju destroy-model my-dns-model --controller lxd --destroy-storage
	juju remove-k8s k8s-cloud --controller lxd

# Debug
debug: # in VM
	juju clouds
	juju controllers
	juju models
	juju status
	juju debug-log
	juju status
