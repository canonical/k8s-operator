VM_NAME = canonical-k8s-charm-vm
CONTROLLER_NAME = lxd
CLUSTER_MODEL = canonical-k8s-model
K8S_CLOUD_NAME = k8s-cloud
K8S_MODEL = dns-model	

.PHONY: setup shell deploy delete refresh deploy_k8s_charm remove_k8s_charm create_k8s_cloud delete_k8s_cloud view fix-profile remove_model add_model clean_vm

# Setup the VM (here multipass) and install juju, lxd, charmcraft, add a model, get git repo
vm:
	multipass launch --name $(VM_NAME) -c 4 -d 40G -m 9G 22.04 
	multipass exec $(VM_NAME) -- sudo apt upgrade -y
	multipass exec $(VM_NAME) -- sudo apt update
	multipass exec $(VM_NAME) -- sudo apt install make	
	multipass exec $(VM_NAME) -- sudo apt install -y snapd
	multipass exec $(VM_NAME) -- sudo snap refresh lxd --channel 5.19
	multipass exec $(VM_NAME) -- sudo snap install juju --classic
	multipass exec $(VM_NAME) -- sudo snap install charmcraft --classic
	multipass exec $(VM_NAME) -- git clone https://github.com/canonical/k8s-operator.git	
	multipass exec $(VM_NAME) -- sudo lxd init --auto
	multipass exec $(VM_NAME) -- lxc network set lxdbr0 ipv6.address=none
	multipass exec $(VM_NAME) -- sudo adduser ubuntu lxd
	multipass exec $(VM_NAME) -- sudo -u ubuntu mkdir -p /home/ubuntu/.local/share/juju
	multipass exec $(VM_NAME) -- sudo -u ubuntu juju bootstrap localhost $(CONTROLLER_NAME)

# Shell into the VM
shell:
	multipass shell $(VM_NAME)

# ALL the following commands should be executed in the k8s-operator dir of the VM
deploy: add_model deploy_k8s_charm create_k8s_cloud

delete: delete_k8s_cloud remove_k8s_charm remove_model

refresh: delete deploy 

add_model:
	juju add-model $(CLUSTER_MODEL)  --config logging-config="<root>=WARNING; unit=DEBUG"

remove_model:
	juju destroy-model $(CLUSTER_MODEL) --destroy-storage

# K8s charm
deploy_k8s_charm:
	juju switch $(CLUSTER_MODEL)
	charmcraft clean -p ./charms/worker/k8s
	charmcraft pack -p ./charms/worker/k8s
	juju deploy ./k8s_ubuntu-20.04-amd64_ubuntu-22.04-amd64.charm 

# until the lxd profile is OK, run after deploy_k8s_charm
MACHINE_ID = 0
#from juju status
fix-profile:
	juju exec --unit k8s/$(MACHINE_ID) echo '--conntrack-max-per-core=0' | sudo tee -a /var/snap/k8s/common/args/kube-proxy
	juju exec --unit k8s/$(MACHINE_ID) sudo snap restart k8s.kube-proxy
	juju exec --unit k8s/$(MACHINE_ID) sudo k8s enable network
	juju exec --unit k8s/$(MACHINE_ID) sudo k8s enable dns
	juju exec --unit k8s/$(MACHINE_ID) sudo k8s enable storage
	juju exec --unit k8s/$(MACHINE_ID) sudo k8s status
	juju exec --unit k8s/$(MACHINE_ID) sudo reboot
	juju exec --unit k8s/$(MACHINE_ID) sudo snap start k8s

remove_k8s_charm: 
	juju switch $(CLUSTER_MODEL)
	juju remove-application k8s	--force
	charmcraft clean -p ./charms/worker/k8s

# K8s cloud
#copy kubeconfig? sudo k8s config view --raw > kubeconfig, change the server to the IP of the k8s master
create_k8s_cloud:
	juju add-k8s $(K8S_CLOUD_NAME) --controller $(CONTROLLER_NAME) --client --skip-storage
	juju add-model --controller $(CONTROLLER_NAME) $(K8S_MODEL) $(K8S_CLOUD_NAME)  --config logging-config="<root>=WARNING; unit=DEBUG"

delete_k8s_cloud: 
	juju destroy-model $(K8S_MODEL) --destroy-storage
	juju remove-k8s $(K8S_CLOUD_NAME) --controller $(CONTROLLER_NAME) --client

deploy-core-dns:
	juju switch $(K8S_MODEL)
	juju deploy coredns --trust
	juju offer coredns:dns-provider

remove-core-dns:
	juju switch $(K8S_MODEL)
	juju remove-application coredns

consume-core-dns:
	juju consume -m $(CLUSTER_MODEL) $(K8S_MODEL).coredns
	juju relate -m $(CLUSTER_MODEL) coredns k8s

delete-dns-relation:
	juju remove-relation -m $(CLUSTER_MODEL) coredns k8s

test-dns:
	juju switch $(CLUSTER_MODEL)
	juju exec --unit k8s/0 -- k8s kubectl run --rm -it --image alpine --restart=Never test-dns -- nslookup canonical.com
# Debug
view: 
	juju clouds
	juju controllers
	juju models
	juju status

# Clean up the VM
clean_vm:
	multipass delete $(VM_NAME)
	multipass purge