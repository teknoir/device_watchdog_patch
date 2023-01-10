# Teknoir Device Watchdog patch
Teknoir Watchdog = Keep-alive scripts for the device
The patch is applied with Ansible

> Ofc you need to have Ansible installed!
> And you need kubernetes credentials

## Limitations
* As namespaces become groups, we do not support namespaces with dashes(-).
  * Dashes(-) will be replaced with underscores(_)
* You have to set kubectl context before running ansible commands.
* The Connection Plugin automatically enable tunnels for devices but...
  * does not tear them down after
    
## List devices
```bash
ansible -i inventory.py --list-hosts all
```

## Patch devices
```bash
ansible-playbook -v -i inventory.py subsystems_patch_playbook.yaml --limit avangard_production-hd-vm-00016,avangard_production-hd-vm-00034,avangard_production-hd-vm-00078
```

# Patched devices
## hd-vm-00002
## hd-vm-00003
hd-vm-00004
hd-vm-00005
hd-vm-00006
hd-vm-00007
hd-vm-00008
hd-vm-00009
## hd-vm-00010
hd-vm-00011
hd-vm-00012
## hd-vm-00013
hd-vm-00014
## hd-vm-00015
hd-vm-00016
## hd-vm-00017
## hd-vm-00018
hd-vm-00019
hd-vm-00020
## hd-vm-00021
hd-vm-00022
hd-vm-00023
hd-vm-00024
hd-vm-00025
hd-vm-00026
hd-vm-00027
hd-vm-00028
hd-vm-00029
hd-vm-00030
hd-vm-00031
hd-vm-00032
hd-vm-00033
hd-vm-00034
## hd-vm-00035
hd-vm-00036
hd-vm-00037
hd-vm-00038
## hd-vm-00039
## hd-vm-00040
## hd-vm-00041
hd-vm-00042
## hd-vm-00043
hd-vm-00044
hd-vm-00045
hd-vm-00046
hd-vm-00047
## hd-vm-00048
## hd-vm-00049
hd-vm-00050
## hd-vm-00051
hd-vm-00052
hd-vm-00053
hd-vm-00054
hd-vm-00055
## hd-vm-00056
hd-vm-00057
hd-vm-00058
hd-vm-00059
hd-vm-00060
hd-vm-00061
hd-vm-00062
hd-vm-00063
hd-vm-00064
hd-vm-00065
hd-vm-00066
hd-vm-00067
hd-vm-00068
hd-vm-00069
hd-vm-00070
hd-vm-00071
hd-vm-00072
hd-vm-00073
## hd-vm-00074
## hd-vm-00075
hd-vm-00076
## hd-vm-00077
hd-vm-00078
hd-vm-00079
hd-vm-00080
hd-vm-00081


curl ml-pipeline.teknoir:8888/apis/v1beta1/runs?resource_reference_key.type=NAMESPACE\&resource_reference_key.id=demonstrations

IN_CLUSTER_DNS_NAME='ml-pipeline.teknoir.svc.cluster.local:8888' python3 build.py