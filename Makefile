VM_NAME = canonical-k8s-charm-vm
CONTROLLER_NAME = lxd
MODEL_NAME = canonical-k8s-model
K8S_CLOUD_NAME = k8s-cloud
K8S_MODEL_NAME = dns-model	
.PHONY: setup shell deploy clean refresh deploy_k8s_charm remove_k8s_charm create_k8s_cloud delete_k8s_cloud view

# Setup the VM (here multipass) and install juju, lxd, charmcraft, add a model, get git repo
vm:
	multipass launch --name $(VM_NAME) -c 4 -d 40G -m 9G 22.04 
	multipass exec $(VM_NAME) -- sudo apt upgrade -y
	multipass exec $(VM_NAME) -- sudo apt update
	multipass exec $(VM_NAME) -- sudo apt install make	
	multipass exec $(VM_NAME) -- sudo apt install -y snapd
	multipass exec $(VM_NAME) -- sudo snap install juju --classic
	multipass exec $(VM_NAME) -- sudo snap install charmcraft --classic
	multipass exec $(VM_NAME) -- git clone https://github.com/canonical/k8s-operator.git	
	multipass exec $(VM_NAME) -- sudo lxd init --auto
	multipass exec $(VM_NAME) -- sudo adduser ubuntu lxd
	multipass exec $(VM_NAME) -- sudo -u ubuntu mkdir -p /home/ubuntu/.local/share/juju
	multipass exec $(VM_NAME) -- sudo -u ubuntu juju bootstrap localhost $(CONTROLLER_NAME)
	multipass exec $(VM_NAME) -- juju add-model $(MODEL_NAME) --config logging-config="<root>=WARNING; unit=DEBUG"

# Shell into the VM
shell:
	multipass shell $(VM_NAME)

# ALL the following commands should be executed in the k8s-operator dir of the VM
deploy: deploy_k8s_charm create_k8s_cloud

clean: delete_k8s_cloud remove_k8s_charm

refresh: clean deploy 

# K8s charm
deploy_k8s_charm:
	charmcraft clean -p ./charms/worker/k8s
	charmcraft pack -p ./charms/worker/k8s
	juju deploy ./k8s_ubuntu-20.04-amd64_ubuntu-22.04-amd64.charm --trust

remove_k8s_charm: 
	juju remove-application k8s

# K8s cloud
create_k8s_cloud:
	juju add-k8s $(K8S_CLOUD_NAME) --controller $(CONTROLLER_NAME) --client
	juju add-model --controller $(CONTROLLER_NAME) $(K8S_MODEL_NAME) $(K8S_CLOUD_NAME)

delete_k8s_cloud: 
	juju destroy-model $(K8S_MODEL_NAME) --controller $(CONTROLLER_NAME) --destroy-storage
	juju remove-k8s $(K8S_CLOUD_NAME) --controller $(CONTROLLER_NAME)

# Debug
view: 
	juju clouds
	juju controllers
	juju models
	juju status
