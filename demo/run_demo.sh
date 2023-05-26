#!/bin/bash

set -e

GRACE_PERIOD=10
WAIT_PERIOD=5
CLAB_FILE="./demo.clab.yaml"
FCLI_IMG="ghcr.io/srl-labs/nornir-srl:0.1.6"
FCLI="docker run -t --rm -v /etc/hosts:/etc/hosts:ro -v ${PWD}/$CLAB_FILE:/topo.yml $FCLI_IMG -t /topo.yml"
FCLI_ALL_ARGS=""
FCLI_FAB_ARGS="-i fabric_node=yes"

interrupt_handler() {
	echo -e "\n\nDon't forget to destroy lab with 'sudo clab destroy -t demo.clab.yaml'."
	echo "Feel free to explore 'fcli' from this directory"
    echo "source .aliases.rc"
    echo "fcli <report>"
	exit 0
}

trap interrupt_handler SIGINT

spin () {
  rotations=$1
  delay=0.5
  for (( i=$1; i>0 ; i-- )); do
    for char in '|' '/' '-' '\'; do
      #'# inserted to correct broken syntax highlighting
      foo=$(printf "%02d" $i)
      echo -n $foo$char
      sleep $delay
      printf "\b\b\b"
    done
  done
}

sudo clab deploy -c -t ./demo.clab.yaml
echo "Waiting $GRACE_PERIOD spins to allow control plane to settle"
spin $GRACE_PERIOD

while true ; do
for report in sys-info lldp-nbrs bgp-peers mac-table nwi-itfs ; do
	clear
	case $report in
	"lldp-nbrs")
		cmd="$FCLI $FCLI_ALL_ARGS $report -f interface=ethernet*"
		echo "\$ fcli $FCLI_ALL_ARGS $report -f interface=ethernet*"
		$cmd
		;;
	"bgp-peers")
		cmd="$FCLI $FCLI_ALL_ARGS $report -i role=spine"
		echo "\$ fcli $FCLI_ALL_ARGS $report -i role=spine"
		$cmd
		;;
	"nwi-itfs")
		cmd="$FCLI $FCLI_ALL_ARGS $FCLI_FAB_ARGS $report -f type=*vrf"
		echo "\$ fcli $FCLI_ALL_ARGS $FCLI_FAB_ARGS $report -f type=*vrf"
		$cmd
		;;
	*)
		cmd="$FCLI $FCLI_ALL_ARGS $FCLI_FAB_ARGS $report"
		echo "\$ fcli $FCLI_ALL_ARGS $FCLI_FAB_ARGS $report"
		$cmd
		;;
	esac
	spin $WAIT_PERIOD
done
for rt in 1 2 3 4 5 ; do
	clear
	case $rt in
	"5")
		cmd="$FCLI $FCLI_ALL_ARGS $FCLI_FAB_ARGS bgp-rib -r route_fam=evpn -r route_type=5 -f 0_st=u*>"
		echo "\$ fcli $FCLI_ALL_ARGS $FCLI_FAB_ARGS bgp-rib -r route_fam=evpn -r route_type=5 -f 0_st=u*>"
		$cmd
		;;
	"2")
		cmd="$FCLI $FCLI_ALL_ARGS $FCLI_FAB_ARGS bgp-rib -r route_fam=evpn -r route_type=2 -f vni=202 -f 0_st=u*>"
		echo "\$ fcli $FCLI_ALL_ARGS $FCLI_FAB_ARGS bgp-rib -r route_fam=evpn -r route_type=2 -f vni=202 -f 0_st=u*>"
		$cmd
		;;
	*)
		cmd="$FCLI $FCLI_ALL_ARGS $FCLI_FAB_ARGS bgp-rib -r route_fam=evpn -r route_type=$rt"
		echo "\$ fcli $FCLI_ALL_ARGS $FCLI_FAB_ARGS bgp-rib -r route_fam=evpn -r route_type=$rt"
		$cmd
		;;
	esac
	spin $WAIT_PERIOD
done
clear
cmd="$FCLI $FCLI_ALL_ARGS $FCLI_FAB_ARGS bgp-rib -r route_fam=ipv4 -f Pfx=192.168.255.2/32"
echo "\$ fcli $FCLI_ALL_ARGS $FCLI_FAB_ARGS bgp-rib -r route_fam=ipv4 -f Pfx=192.168.255.2/32"
$cmd
spin $WAIT_PERIOD
done
