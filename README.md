# MetaStack

A lean, mean cloud computing platform experiment...

## How

The platform has:

- Compute nodes that run VMs (KVM hypervisor)
- An HA storage cluster (Ceph) - all VM disk storage is remote
- An HA distributed database (etcd) that stores:
  - platform configuration
  - desired VM state (updated by management nodes)
  - actual VM state (updated by compute nodes)
- One or more management nodes that provide CLI and web browser
  interfaces for managing VMs
- Centralised diagnostics via syslog

## Operation

- The management interface requests a new VM by adding an entry to
  the "desired VMs" section of the etcd database.
- The compute nodes all monitor the database. When they spot
  a new VM request, if they are capable and want to run it they
  claim it via an atomic compare/write operation. Only one compute
  node can win.
- To destroy a VM, the management interface removes the VM entry
  from the "desired VMs".  The compute node running the VM spots
  this and destroys the VM.
- Compute nodes publish their state to the etcd database, and
  this can be viewed via the web or CLI interfaces.
- Compute node VM state database entries have a time-to-live
  value.  If the compute node dies these entries expire within
  seconds, then the other compute nodes notice that the VM is no
  longer running and fire up a new one using the same disk image.

One novel feature is that there is no centralised VM scheduler - all the compute nodes take an independent decision on whether they want to try and run VMs. This could be considered genius or insanity.  I haven't made up my mind yet.  But the architecture does not preclude manually assigning VMs to nodes, or having an additional scheduler component.

Google Kubernetes (Docker container manager) uses etcd in a very similar way (but with a scheduler component).

## Install

We should automate install using Ansible playbooks (or similar) to make it trivial to add/configure new nodes. For now, you'll have to do it manually.

### Compute node install

- Install minimal CentOS
- Install KVM: http://xmodulo.com/install-configure-kvm-centos.html
- Install etcd (see below)
- Install python-etcd:

        yum install git gcc python-devel libffi-devel openssl-devel
        git clone https://github.com/jplana/python-etcd.git
        cd python-etcd/
        python setup.py install

- Copy metastack.py from git
- Run metastack.py

        python metastack.py

### etcd install

Need to do this on etcd nodes and compute nodes.

- Follow the instructions at https://github.com/coreos/etcd/releases/
- Copy the resulting folder into /opt/
- Create softlinks to etcd and etcdctl on the path.

Install dependencies:

        yum install -y libffi-devel
        pip install -e .

Create a softlink to bin/run_proxy on the path.

### Ceph storage cluster install

Perform the following on the ceph nodes:

- http://ceph.com/docs/master/start/quick-start-preflight/
- http://ceph.com/docs/master/start/quick-ceph-deploy/

The following should then be performed to install ceph on the client nodes:

- ssh-copy-id <client IP>
- ceph-deploy install <client IP>
- ceph-deploy admin <client IP>

To get libvirt to use the admin key for ceph (other users can be created and used,
but for the hackathon I simplified this by just using admin) the following should
be performed:

        cat > secret.xml <<EOF
        <secret ephemeral='no' private='no'>
                <usage type='ceph'>
                        <name>client.admin secret</name>
                </usage>
        </secret>
        EOF

        sudo virsh secret-define --file secret.xml
        <uuid of secret is output here>

        ceph auth get-key client.admin | sudo tee client.admin.key

        sudo virsh secret-set-value --secret {uuid of secret} --base64 $(cat client.admin.key)

        virsh edit <domain> in the disk section set the following:
          </source>
                <auth username='admin'>
                        <secret usage='client.admin secret'/>
                </auth>
                <target…


