#!/usr/bin/env python

# metastack.py
#
# A very lightweight virtual machine platform.
# Dependencies:
# - virsh for managing VMs
# - etcd distributed database for control and status
# - ceph disk cluster for storage

# Copyright 2015 John Batty
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import socket
import etcd
import time
import uuid
import subprocess
import jinja2
import logging
import json
import logging.handlers

SYSLOG_SERVER = '10.225.24.230'
SYSLOG_UDP_PORT = 514
SYSLOG_FACILITY_LOCAL0 = 16

# Lifetime of values published in etcd
ETCD_EXPIRY_PERIOD = 5

vm_definition_template = """
<domain type='kvm' id='2'>
  <name>{{ name }}</name>
  <uuid>{{ uuid }}</uuid>
  <metadata/>
  <memory unit='KiB'>{{ ram }}</memory>
  <currentMemory unit='KiB'>{{ ram }}</currentMemory>
  <vcpu placement='static'>{{ num_vcpu }}</vcpu>
  <resource>
    <partition>/machine</partition>
  </resource>
  <os>
    <type arch='x86_64' machine='pc-i440fx-rhel7.0.0'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
    <pae/>
  </features>
  <cpu mode='custom' match='exact'>
    <model fallback='allow'>Westmere</model>
  </cpu>
  <clock offset='utc'>
    <timer name='rtc' tickpolicy='catchup'/>
    <timer name='pit' tickpolicy='delay'/>
    <timer name='hpet' present='no'/>
  </clock>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>restart</on_crash>
  <devices>
    <emulator>/usr/libexec/qemu-kvm</emulator>
    <disk type='network' device='disk'>
      <driver name='qemu' type='raw'/>
      <auth username='admin'>
        <secret type='ceph' usage='client.admin secret'/>
      </auth>
      <source protocol='rbd' name='{{ volume_name }}'/>
      <backingStore/>
      <target dev='vda' bus='virtio'/>
      <alias name='virtio-disk0'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x06' function='0x0'/>
    </disk>
    <controller type='usb' index='0' model='ich9-ehci1'>
      <alias name='usb0'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x04' function='0x7'/>
    </controller>
    <controller type='usb' index='0' model='ich9-uhci1'>
      <alias name='usb0'/>
      <master startport='0'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x04' function='0x0' multifunction='on'/>
    </controller>
    <controller type='usb' index='0' model='ich9-uhci2'>
      <alias name='usb0'/>
      <master startport='2'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x04' function='0x1'/>
    </controller>
    <controller type='usb' index='0' model='ich9-uhci3'>
      <alias name='usb0'/>
      <master startport='4'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x04' function='0x2'/>
    </controller>
    <controller type='pci' index='0' model='pci-root'>
      <alias name='pci.0'/>
    </controller>
    <interface type='network'>
      <mac address='{{ mac_addr }}'/>
      <source network='host-bridge'/>
      <target dev='vnet0'/>
      <model type='virtio'/>
      <alias name='net0'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x03' function='0x0'/>
    </interface>
    <serial type='pty'>
      <source path='/dev/pts/2'/>
      <target port='0'/>
      <alias name='serial0'/>
    </serial>
    <console type='pty' tty='/dev/pts/2'>
      <source path='/dev/pts/2'/>
      <target type='serial' port='0'/>
      <alias name='serial0'/>
    </console>
    <input type='tablet' bus='usb'>
      <alias name='input0'/>
    </input>
    <memballoon model='virtio'>
      <alias name='balloon0'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x05' function='0x0'/>
    </memballoon>
  </devices>
</domain>
""".strip()


def host_name():
    return socket.gethostname()


def host_ip_addr():
    return socket.gethostbyname(host_name())


def cmd(command):
    logger.info("> %s" % command)
    output = subprocess.check_output(command, shell=True)
    clean_output = output.strip()
    if clean_output:
        logger.info("| %s" % clean_output)


def save_file(filename, data):
    f = open(filename, "w")
    f.write(data)
    f.close()


class Deployment:
    def __init__(self):
        self.etcd = etcd.Client()
        self.desired_vms = {}
        self.actual_vms = {}
        self.my_vms = {}
        self.host_id = host_name()
        self.host_name = host_name()
        self.total_ram = 8192
        self.used_ram = 0
        self.total_vcpu = 8
        self.used_vcpu = 0
        self.vm_count = 0
        # self.write_syslog_server()

    def write_syslog_server(self):
        self.etcd.write("/metastack/deployment_config/syslog_server",
                        SYSLOG_SERVER)

    def register_host(self):
        host_info = '{ "name": "%s", "total_ram": %d, "used_ram": %d, ' \
                    '"total_vcpu": %d, "used_vcpu": %d }' % (
                        self.host_name,
                        self.total_ram,
                        self.used_ram,
                        self.total_vcpu,
                        self.used_vcpu)

        self.etcd.write("/metastack/hosts/%s" % self.host_id,
                        host_info,
                        ttl=ETCD_EXPIRY_PERIOD)

    def create_vm_volume(self, vm_id):
        volume_name = "vm-%s-0" % vm_id
        logger.info("Creating VM volume: %s" % volume_name)
        try:
            cmd("qemu-img convert "
                "rbd:images/cirros-0.3.3-x86_64-disk.raw rbd:volumes/%s" %
                volume_name)
            logger.info("Created VM volume: %s" % volume_name)
        except subprocess.CalledProcessError:
            logger.error("Failed to created VM volume: %s" % volume_name)

        return volume_name

    def delete_vm_volume(self, volume_name):
        cmd("rbd rm volumes/%s" % volume_name)

    def create_vm(self, vm_id, vm_info, volume_name):
        vm_uuid = uuid.uuid1()
        safe_vm_name = ("%s-%s" % (vm_id, vm_info["name"])).replace(" ", "-")
        ctx = {
            "name": safe_vm_name,
            "uuid": vm_uuid,
            "ram": 1024 * 1024,
            "num_vcpu": 1,
            "volume_name": "volumes/%s" % volume_name,
            "mac_addr": "00:50:03:%02x:%02x:%02x" % (
                (self.vm_count / (256*256)) % 255,
                (self.vm_count / 256) % 255,
                self.vm_count % 255)
        }
        template = jinja2.Template(vm_definition_template)
        vm_definition = template.render(ctx)
        logger.debug("vm_definition:\n%s" % vm_definition)
        save_file("vmdef.xml", vm_definition)
        cmd("virsh define vmdef.xml")
        cmd("virsh start %s" % vm_uuid)
        self.vm_count += 1
        return vm_uuid

    def run_vm(self, vm_id, vm_info):
        logger.info("Running VM: vm_id=%s. vm_info=%s" % (vm_id, vm_info))
        try:
            volume_name = self.create_vm_volume(vm_id)
            vm_uuid = self.create_vm(vm_id, vm_info, volume_name)
            self.my_vms[vm_id] = vm_info
            self.my_vms[vm_id]["vm_uuid"] = vm_uuid
            self.my_vms[vm_id]["volume_name"] = volume_name
        except:
            logger.error("Failed to create VM")
            raise

    def maybe_run_vm(self, vm_id, vm_info):
        logger.info("Pending VM request: vm_id=%s. vm_info=%s" % (
            vm_id, vm_info))
        try:
            # Claim the VM
            logger.info("Trying to claim VM request: vm_id=%s. vm_info=%s" % (
                vm_id, vm_info))
            actual_vm_info = "{ host: %s, state: start }" % self.host_id
            self.etcd.write("/metastack/actual_vms/%s" % vm_id,
                            actual_vm_info,
                            prevExist=False,
                            ttl=ETCD_EXPIRY_PERIOD)
            self.run_vm(vm_id, vm_info)
        except etcd.EtcdAlreadyExist:
            vm_info = self.etcd.read("/metastack/actual_vms/%s" % vm_id)
            logger.info("Grrr.. Another host beat me to it! %s" % vm_info)

    def delete_vm(self, vm_id):
        logger.info("Deleting VM: vm_id=%s" % vm_id)
        vm_info = self.my_vms[vm_id]
        vm_uuid = vm_info["vm_uuid"]
        cmd("virsh destroy %s" % vm_uuid)
        cmd("virsh undefine %s" % vm_uuid)
        self.delete_vm_volume(vm_info["volume_name"])
        del self.my_vms[vm_id]

    def delete_all_vms(self):
        vm_ids = self.my_vms.keys()
        for vm_id in vm_ids:
            self.delete_vm(vm_id)

    def etcd_items(self, path):
        children = self.etcd.read(path)._children
        items = {}
        for child in children:
            key = child["key"]
            short_key = key.split("/")[-1]
            items[short_key] = self.etcd.read(key).value
        return items

    def poll_state(self):
        desired_vms = self.etcd_items("/metastack/desired_vms")
        actual_vms = self.etcd_items("/metastack/actual_vms")
        logger.debug(desired_vms)
        logger.debug(actual_vms)
        for vm_id, vm_info in desired_vms.iteritems():
            if vm_id not in actual_vms:
                self.maybe_run_vm(vm_id, json.loads(vm_info))
        for vm_id, vm_info in self.my_vms.items():
            if vm_id not in desired_vms:
                self.delete_vm(vm_id)

    def check_vm_states(self):
        pass

    def publish_state(self):
        for vm_id, vm_info in self.my_vms.iteritems():
            vm_info_str = '{ "host": "%s", "state": "%s", "vm_uuid": "%s" }' % \
                (self.host_id, "start", vm_info["vm_uuid"])
            self.etcd.write("/metastack/actual_vms/%s" % vm_id,
                            vm_info_str,
                            ttl=ETCD_EXPIRY_PERIOD)


def init_logging():
    logger = logging.getLogger("MetaStack")
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '%(asctime)s %(name)s ' + host_name() + ' %(levelname)-8s %(message)s',
        datefmt="%Y-%m-%d %H:%M:%S")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    syslog_handler = logging.handlers.SysLogHandler(
        address=(SYSLOG_SERVER, SYSLOG_UDP_PORT),
        facility=SYSLOG_FACILITY_LOCAL0)
    syslog_handler.setFormatter(formatter)
    logger.addHandler(syslog_handler)
    return logger


def main():
    deployment = Deployment()
    logger.info("Metastack starting...")
    try:
        while True:
            deployment.register_host()
            deployment.poll_state()
            deployment.publish_state()
            time.sleep(2)
    except:
        logger.info("Metastack terminating")
        deployment.delete_all_vms()
        raise


if __name__ == '__main__':
    logger = init_logging()
    main()
