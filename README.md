# Teknoir Device Watchdog patch
Teknoir Watchdog = Keep-alive scripts for the device
The patch is applied with Ansible

For Teknoir Ansible plugins see:
* https://github.com/teknoir/ansible (running from local)
* https://github.com/teknoir/ansible-notebook (running from notebook, already installed)
* https://github.com/teknoir/ansible-kubeflow (running from kubeflow)

The patch is meant to be run with:
* https://github.com/teknoir/device-patch-workflow

To run:
1. Create a flow in a devstudio, starting with a "inject"-node to a "function"-node with:
```javascript
msg.payload = { "args": {
    "playbook_git_repo": "https://github.com/teknoir/device_watchdog_patch.git",
    "playbook_path": "playbook-watchdog_patch.yaml",
    "ansible_limit": "rpi4-8gb-black",
    "add_device_label": "watchdog=enabled"
}};
return msg;
```
2. Connect that to a run pipeline node where you select the "Device Patch Workflow"-pipeline.
3. Deploy the flow!
4. And trigger the job with the "inject"-node.

## List devices
```bash
ansible --list-hosts all
```

## Patch devices from a notebook
```bash
ansible-playbook -v -i inventory.py playbook-watchdog_patch.yaml --limit avangard_production-hd-vm-00016,avangard_production-hd-vm-00034,avangard_production-hd-vm-00078
```
