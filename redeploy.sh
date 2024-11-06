set -x

DIR=$(realpath $(dirname "${0}"))
LB_DIR=$DIR/../k8s-load-balancer

echo "Run charmcraft pack for k8s"
charmcraft pack -p charms/worker/k8s

echo "Run charmcraft pack for k8s-load-balancer"
cd $LB_DIR
charmcraft pack

cd $DIR

echo "Removing k8s k8s-load-balancer relation..."
juju remove-relation k8s k8s-load-balancer

echo "Removing k8s-load-balancer application..."
juju remove-application k8s-load-balancer

echo "Refresh k8s application with new charm"
juju refresh k8s --switch $DIR/k8s_ubuntu-20.04-amd64_ubuntu-22.04-amd64_ubuntu-24.04-amd64.charm --force-units --force

echo "Refresh k8s-load-balancer application with new charm"
juju deploy $LB_DIR/k8s-load-balancer_ubuntu-22.04-amd64.charm

echo "Add relation"
juju integrate k8s k8s-load-balancer
