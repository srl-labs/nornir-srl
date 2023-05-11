#!/bin/bash

set -e
GRACE_PERIOD=10
WAIT_PERIOD=5
interrupt_handler() {
	echo -e "\n\nDon't forget to destroy lab with 'sudo clab destroy -t demo.clab.yaml'."
	echo "Feel free to explore 'fcli' from this directory"
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
if ! command -v fcli >/dev/null 2>&1; then
	echo "fcli command is not installed. Exitting..."
	exit 1
fi
sudo clab deploy -c -t ./demo.clab.yaml
echo "Waiting $GRACE_PERIOD spins to allow control plane to settle"
spin $GRACE_PERIOD

while true ; do
for report in sys-info lldp-nbrs bgp-peers mac-table nwi-itfs ; do
	clear
	case $report in
	"lldp-nbrs")
		cmd="fcli $report -f interface=ethernet"
		echo "\$ $cmd"
		$cmd
		;;
	"bgp-peers")
		cmd="fcli $report -i role=spine"
		echo "\$ $cmd"
		$cmd
		;;
	"nwi-itfs")
		cmd="fcli $report -f type=vrf"
		echo "\$ $cmd"
		$cmd
		;;
	*)
		cmd="fcli $report"
		echo "\$ $cmd"
		$cmd
		;;
	esac
	spin $WAIT_PERIOD
done
for rt in 1 2 3 4 5 ; do
	clear
	case $rt in
	"5")
		cmd='fcli bgp-rib -r route_fam=evpn -r route_type=5 -f 0_st=u*>'
		echo "\$ $cmd"
		$cmd
		;;
	"2")
		cmd='fcli bgp-rib -r route_fam=evpn -r route_type=2 -f vni=202 -f 0_st=u*>'
		echo "\$ $cmd"
		$cmd
		;;
	*)
		cmd="fcli bgp-rib -r route_fam=evpn -r route_type=$rt"
		echo "\$ $cmd"
		$cmd
		;;
	esac
	spin $WAIT_PERIOD
done
clear
cmd="fcli bgp-rib -r route_fam=ipv4 -f Pfx=192.168.255.2/32"
echo "\$ $cmd"
$cmd
spin $WAIT_PERIOD
done
