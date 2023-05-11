#!/bin/bash

set -e
GRACE_PERIOD=10
WAIT_PERIOD=5
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
		fcli $report -f interface=ethernet
		;;
	"bgp-peers")
		fcli $report -i role=spine
		;;
	"nwi-itfs")
		fcli $report -f type=vrf
		;;
	*)
		fcli $report
		;;
	esac
	spin $WAIT_PERIOD
done
for rt in 1 2 3 4 5 ; do
	clear
	case $rt in
	"5")
		fcli bgp-rib -r route_fam=evpn -r route_type=$rt -f 0_st="u*>"
		;;
	*)
		fcli bgp-rib -r route_fam=evpn -r route_type=$rt
		;;
	esac
	spin $WAIT_PERIOD
done
clear
fcli bgp-rib -r route_fam=ipv4 
spin $WAIT_PERIOD
done
