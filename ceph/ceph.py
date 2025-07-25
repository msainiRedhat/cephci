"""This module implements the required foundation data structures for testing."""

import codecs
import datetime
import json
import pickle
import random
import re
import socket
from distutils.version import LooseVersion
from time import sleep, time

import cryptography
import paramiko
import requests
import yaml

from ceph.parallel import parallel
from cli.ceph.ceph import Ceph as CephCli
from utility import lvm_utils
from utility.log import Log
from utility.utils import custom_ceph_config

logger = Log(__name__)


class SocketTimeoutException(Exception):
    pass


class ResourceNotFoundError(Exception):
    pass


class Ceph(object):
    DEFAULT_RHCS_VERSION = "4.3"

    def __init__(self, name, node_list=None):
        """
        Ceph cluster representation. Contains list of cluster nodes.
        Args:
            name (str): cluster name
            node_list (ceph.utils.CephVMNode): CephVMNode list
        """
        self.name = name
        self.node_list = list(node_list)
        self.use_cdn = False
        self.custom_config_file = None
        self.custom_config = None
        self.allow_custom_ansible_config = True
        self.__rhcs_version = None
        self.ceph_nodename = None
        self.networks = dict()

    def __eq__(self, ceph_cluster):
        if hasattr(ceph_cluster, "node_list"):
            if all(atomic_node in ceph_cluster for atomic_node in self.node_list):
                return True
            else:
                return False
        else:
            return False

    def __ne__(self, ceph_cluster):
        return not self.__eq__(ceph_cluster)

    def __len__(self):
        return len(self.node_list)

    def __getitem__(self, key):
        return self.node_list[key]

    def __setitem__(self, key, value):
        self.node_list[key] = value

    def __delitem__(self, key):
        del self.node_list[key]

    def __iter__(self):
        return iter(self.node_list)

    @property
    def rhcs_version(self):
        """
        Get rhcs version, will return DEFAULT_RHCS_VERSION if not set
        Returns:
            LooseVersion: rhcs version of given cluster

        """
        return LooseVersion(
            str(
                self.__rhcs_version
                if self.__rhcs_version
                else self.DEFAULT_RHCS_VERSION
            )
        )

    @rhcs_version.setter
    def rhcs_version(self, version):
        self.__rhcs_version = version
        luminous_demons = self.get_ceph_objects("mgr") + self.get_ceph_objects("nfs")
        for luminous_demon in luminous_demons:  # type: CephDemon
            luminous_demon.is_active = False if self.rhcs_version < "3" else True

    def get_nodes(self, role=None, ignore=None):
        """
        Get node(s) by role. Return all nodes if role is not defined
        Args:
            role (str, RolesContainer): node's role. Takes precedence over ignore
            ignore (str, RolesContainer): node's role to ignore from the list

        Returns:
            list: nodes
        """
        if role:
            return [node for node in self.node_list if node.role == role]
        elif ignore:
            return [node for node in self.node_list if node.role != ignore]
        else:
            return list(self.node_list)

    def get_ceph_objects(self, role=None):
        """
        Get Ceph Object by role.

        Returns all objects if role is not defined. Ceph object can be Ceph demon,
        client, installer or generic entity. Pool role is never assigned to Ceph object
        and means that node has no Ceph objects

        Args:
            role (str): Ceph object's role as str

        Returns:
            list: ceph objects
        """
        node_list = self.get_nodes(role)
        ceph_object_list = []
        for node in node_list:
            ceph_object_list.extend(node.get_ceph_objects(role))
        return ceph_object_list

    def get_ceph_object(self, role, order_id=0):
        """
        Returns single ceph object.

        If order id is provided returns that occurrence from results list, otherwise
        returns first occurrence

        Args:
            role(str): Ceph object's role
            order_id(int): order number of the ceph object

        Returns:
            CephObject: ceph object

        """
        try:
            return self.get_ceph_objects(role)[order_id]
        except IndexError:
            return None

    def setup_ceph_firewall(self):
        """
        Open required ports on nodes based on relevant ceph demons types
        """
        for node in self.get_nodes():
            ports = list()
            if node.role == "mon":
                ports += ["6789"]

                # for upgrades from 2.5 to 3.x, we convert mon to mgr
                # so lets open ports from 6800 to 6820
                ports += ["6800-6820"]

            if node.role == "osd":
                ports += ["6800-7300"]

            if node.role == "mgr":
                ports += ["6800-6820"]

            if node.role == "mds":
                ports += ["6800"]

            if node.role == "iscsi-gw":
                ports += ["3260", "5000-5001"]

            if node.role == "grafana":
                ports += ["6800-6820"]

            if ports:
                node.configure_firewall()
                node.open_firewall_port(port=ports, protocol="tcp")

    def setup_ssh_keys(self):
        """
        Generate and distribute ssh keys within cluster
        """
        keys = ""
        hosts = ""
        hostkeycheck = (
            "Host *\n\tStrictHostKeyChecking no\n\tServerAliveInterval 2400\n"
        )
        for ceph in self.get_nodes():
            ceph.generate_id_rsa()
            keys = keys + ceph.id_rsa_pub
            hosts = (
                hosts
                + ceph.ip_address
                + "\t"
                + ceph.hostname
                + "\t"
                + ceph.shortname
                + "\n"
            )
        for ceph in self.get_nodes():
            keys_file = ceph.remote_file(
                file_name=".ssh/authorized_keys", file_mode="a"
            )
            hosts_file = ceph.remote_file(
                sudo=True, file_name="/etc/hosts", file_mode="a"
            )
            ceph.exec_command(
                cmd="[ -f ~/.ssh/config ] && chmod 700 ~/.ssh/config", check_ec=False
            )
            ssh_config = ceph.remote_file(file_name=".ssh/config", file_mode="a")
            keys_file.write(keys)
            hosts_file.write(hosts)
            ssh_config.write(hostkeycheck)
            keys_file.flush()
            hosts_file.flush()
            ssh_config.flush()
            ceph.exec_command(cmd="chmod 600 ~/.ssh/authorized_keys")
            ceph.exec_command(cmd="chmod 400 ~/.ssh/config")

    def generate_ansible_inventory(
        self, device_to_add=None, mixed_lvm_confs=None, filestore=False
    ):
        """
        Generate ansible inventory file content for given cluster
        Args:
            device_to_add(str): To add new osd to the cluster, default None
            mixed_lvm_confs(str): To configure multiple mixed lvm configs, default None
            filestore(bool): True for filestore usage, dafault False
        Returns:
            str: inventory

        """
        mon_hosts = []
        osd_hosts = []
        rgw_hosts = []
        mds_hosts = []
        mgr_hosts = []
        nfs_hosts = []
        client_hosts = []
        iscsi_gw_hosts = []
        grafana_hosts = []
        counter = 0

        for node in self:  # type: CephNode
            eth_interface = node.search_ethernet_interface(self)
            if eth_interface is None:
                err = "Network test failed: No suitable interface is found on {node}.".format(
                    node=node.ip_address
                )
                logger.error(err)
                raise RuntimeError(err)
            node.set_eth_interface(eth_interface)
            mon_interface = " monitor_interface=" + node.eth_interface + " "
            if node.role == "mon":
                mon_host = node.shortname + " monitor_interface=" + node.eth_interface
                mon_hosts.append(mon_host)
                # num_mons += 1
            if node.role == "mgr" and self.rhcs_version >= "3":
                mgr_host = node.shortname + " monitor_interface=" + node.eth_interface
                mgr_hosts.append(mgr_host)
            if node.role == "osd":
                devices = self.get_osd_devices(node)
                self.setup_osd_devices(devices, node)
                auto_discovery = self.ansible_config.get("osd_auto_discovery", False)
                dmcrypt = ""
                objectstore = ""
                if filestore:
                    objectstore = ' osd_objectstore="filestore"' + " "

                if (
                    self.ansible_config.get("osd_scenario") == "lvm"
                    and not mixed_lvm_confs
                ):
                    devices_prefix = "lvm_volumes"
                    devices = node.create_lvm(devices)
                elif (
                    self.ansible_config.get("osd_scenario") == "lvm" and mixed_lvm_confs
                ):
                    """
                    adding new OSD to cluster,shows only 2 disks free,
                    need to change this code after issue gets resolved
                    https://gitlab.cee.redhat.com/ceph/cephci/issues/17
                    """
                    devices_prefix = "lvm_volumes"
                    dmcrypt = ""
                    if "pool" in node.hostname:
                        logger.info(node.hostname)
                        devices = node.create_lvm(
                            (
                                devices[0:1]
                                if not device_to_add
                                else device_to_add.split()
                            ),
                            num=random.randint(1, 10) if device_to_add else None,
                            check_lvm=False if device_to_add else True,
                        )
                    else:
                        osd_scenario = node.osd_scenario or counter
                        lvm_vols = node.multiple_lvm_scenarios(
                            devices, lvm_utils.osd_scenario_list[osd_scenario]
                        )
                        counter += 1
                        logger.info(lvm_vols)
                        devices = '"[' + lvm_vols.get(node.hostname)[0] + ']"'
                        dmcrypt_opt = lvm_vols.get(node.hostname)[1]
                        batch_opt = lvm_vols.get(node.hostname)[2]
                        dmcrypt = (
                            "dmcrypt='True'" + " " if dmcrypt_opt.get("dmcrypt") else ""
                        )
                        devices_prefix = (
                            "devices" if batch_opt.get("batch") else "lvm_volumes"
                        )
                else:
                    devices_prefix = "devices"
                if mixed_lvm_confs and len(devices) > 2:
                    devices = (
                        " {devices_prefix}={devices}".format(
                            devices_prefix=devices_prefix, devices=devices
                        )
                        + " "
                    )
                else:
                    devices = (
                        " {devices_prefix}='{devices}'".format(
                            devices_prefix=devices_prefix, devices=json.dumps(devices)
                        )
                        if not auto_discovery
                        else ""
                    ) + " "
                osd_host = (
                    node.shortname + mon_interface + devices + objectstore + dmcrypt
                )
                osd_hosts.append(osd_host)
            if node.role == "mds":
                mds_host = node.shortname + " monitor_interface=" + node.eth_interface
                mds_hosts.append(mds_host)
            if (
                node.role == "nfs"
                and self.rhcs_version >= "3"
                and node.pkg_type == "rpm"
            ):
                nfs_host = node.shortname + " monitor_interface=" + node.eth_interface
                nfs_hosts.append(nfs_host)
            if node.role == "rgw":
                rgw_host = node.shortname + " radosgw_interface=" + node.eth_interface
                rgw_hosts.append(rgw_host)
            if node.role == "client":
                client_host = node.shortname + " client_interface=" + node.eth_interface
                client_hosts.append(client_host)
            if node.role == "iscsi-gw":
                iscsi_gw_host = node.shortname
                iscsi_gw_hosts.append(iscsi_gw_host)
            if node.role == "grafana":
                grafana_host = (
                    node.shortname + " grafana_interface=" + node.eth_interface
                )
                grafana_hosts.append(grafana_host)
        hosts_file = ""
        if mon_hosts:
            mon = "[mons]\n" + "\n".join(mon_hosts)
            hosts_file += mon + "\n"
        if mgr_hosts:
            mgr = "[mgrs]\n" + "\n".join(mgr_hosts)
            hosts_file += mgr + "\n"
        if osd_hosts:
            osd = "[osds]\n" + "\n".join(osd_hosts)
            hosts_file += osd + "\n"
        if mds_hosts:
            mds = "[mdss]\n" + "\n".join(mds_hosts)
            hosts_file += mds + "\n"
        if nfs_hosts:
            nfs = "[nfss]\n" + "\n".join(nfs_hosts)
            hosts_file += nfs + "\n"
        if rgw_hosts:
            rgw = "[rgws]\n" + "\n".join(rgw_hosts)
            hosts_file += rgw + "\n"
        if client_hosts:
            client = "[clients]\n" + "\n".join(client_hosts)
            hosts_file += client + "\n"
        if iscsi_gw_hosts:
            iscsi_gw = "[iscsigws]\n" + "\n".join(iscsi_gw_hosts)
            hosts_file += iscsi_gw + "\n"
        if grafana_hosts:
            grafana = "[grafana-server]\n" + "\n".join(grafana_hosts)
            hosts_file += grafana + "\n"
        logger.info("Generated hosts file: \n{file}".format(file=hosts_file))
        return hosts_file

    def get_osd_devices(self, node):
        """
        Get osd devices list
        Args:
            node(CephNode): Ceph node with osd demon

        Returns:
            list: devices

        """
        devs = []
        devchar = 98
        if hasattr(node, "vm_node") and node.vm_node.node_type == "baremetal":
            devs = [x.path for x in node.get_allocated_volumes()]
        else:
            devices = len(node.get_allocated_volumes())
            for vol in range(0, devices):
                dev = "/dev/vd" + chr(devchar)
                devs.append(dev)
                devchar += 1

        reserved_devs = []
        collocated = self.ansible_config.get("osd_scenario") == "collocated"
        lvm = self.ansible_config.get("osd_scenario") == "lvm"

        if not collocated and not lvm:
            reserved_devs = [
                raw_journal_device
                for raw_journal_device in set(
                    self.ansible_config.get("dedicated_devices")
                )
            ]

        if len(node.get_free_volumes()) >= len(reserved_devs):
            for _ in reserved_devs:
                node.get_free_volumes()[0].status = NodeVolume.ALLOCATED

        devs = [_dev for _dev in devs if _dev not in reserved_devs]
        return devs

    def setup_osd_devices(self, devices, node):
        # TODO: move to CephNode
        """
        Sets osd devices on a node
        Args:
            devices (list): list of devices (/dev/vdb, /dev/vdc)
            node (CephNode): Ceph node
        """
        devices = list(devices)
        for osd_demon in node.get_ceph_objects("osd"):  # type: CephOsd
            device = devices.pop() if len(devices) > 0 else None
            if device:
                osd_demon.device = device[device.rfind("/") + 1 : :]
            else:
                osd_demon.device = None

    def get_ceph_demons(self, role=None):
        """
        Get Ceph demons list
        Returns:
            list: list of CephDemon

        """
        node_list = self.get_nodes(role)
        ceph_demon_list = []
        for node in node_list:  # type: CephNode
            ceph_demon_list.extend(node.get_ceph_demons(role))
        return ceph_demon_list

    def set_ansible_config(self, ansible_config):
        """
        Set ansible config for all.yml
        Args:
            ansible_config(dict): Ceph Ansible all.yml config
        """
        if self.allow_custom_ansible_config:
            ceph_conf_overrides = ansible_config.get("ceph_conf_overrides")
            custom_config = self.custom_config
            custom_config_file = self.custom_config_file
            ansible_config["ceph_conf_overrides"] = custom_ceph_config(
                ceph_conf_overrides, custom_config, custom_config_file
            )
            logger.info(
                "ceph_conf_overrides: \n{}".format(
                    yaml.dump(
                        ansible_config.get("ceph_conf_overrides"),
                        default_flow_style=False,
                    )
                )
            )
        self.__ansible_config = ansible_config
        self.containerized = self.ansible_config.get("containerized_deployment", False)
        for ceph_demon in self.get_ceph_demons():
            ceph_demon.containerized = True if self.containerized else False
        if self.ansible_config.get("fetch_directory") is None:
            # default fetch directory is not writeable, lets use local one if not set
            self.__ansible_config["fetch_directory"] = "~/fetch/"
        for node in self.get_nodes("osd"):
            devices = self.get_osd_devices(node)
            self.setup_osd_devices(devices, node)

    def get_ansible_config(self):
        """
        Get Ansible config settings for all.yml
        Returns:
            dict: Ansible config

        """
        try:
            self.__ansible_config
        except AttributeError:
            raise RuntimeError("Ceph ansible config is not set")
        return self.__ansible_config

    @property
    def ansible_config(self):
        return self.get_ansible_config()

    @ansible_config.setter
    def ansible_config(self, ansible_config):
        self.set_ansible_config(ansible_config)

    def setup_insecure_registry(self):
        """
        Update all ceph demons nodes to allow insecure registry use
        """
        if self.containerized and self.ansible_config.get("ceph_docker_registry"):
            logger.warning(
                "Adding insecure registry:\n{registry}".format(
                    registry=self.ansible_config.get("ceph_docker_registry")
                )
            )

    @property
    def ceph_demon_stat(self):
        """
        Retrieves expected numbers for demons of each role
        Returns:
            dict: Ceph demon stats
        """
        ceph_demon_counter = {}
        for demon in self.get_ceph_demons():
            if demon.role == "mgr" and self.rhcs_version < "3":
                continue
            increment = (
                1  # len(self.get_osd_devices(demon.node)) if demon.role == 'osd' else 1
            )
            ceph_demon_counter[demon.role] = (
                ceph_demon_counter[demon.role] + increment
                if ceph_demon_counter.get(demon.role)
                else increment
            )
        return ceph_demon_counter

    @property
    def ceph_stable_release(self):
        """
        Retrieve ceph stable realease based on ansible config (jewel, luminous, etc.)
        Returns:
            str: Ceph stable release
        """
        return self.ansible_config["ceph_stable_release"]

    def get_metadata_list(self, role, client=None):
        """
        Returns metadata for demons of specified role
        Args:
            role(str): ceph demon role
            client(CephObject): Client with keyring and ceph-common

        Returns:
            list: metadata as json object representation
        """
        if not client:
            client = (
                self.get_ceph_object("client")
                if self.get_ceph_object("client")
                else self.get_ceph_object("mon")
            )

        out, err = client.exec_command(
            cmd=f"ceph {role} metadata -f json-pretty", sudo=True
        )
        return json.loads(out)

    def get_osd_metadata(self, osd_id, client=None):
        """
        Returns metadata for osd by given id

        Args:
            osd_id (int): osd id
            client (CephObject): Client with keyring and ceph-common

        Returns:
            metadata (Dict): osd metadata

        Example::

             {
                "id": 8,
                "arch": "x86_64",
                "back_addr": "172.16.115.29:6801/1672",
                "back_iface": "eth0",
                "backend_filestore_dev_node": "vdd",
                "backend_filestore_partition_path": "/dev/vdd1",
                "ceph_version": "ceph version 12.2.5-42.el7cp
                                 (82d52d7efa6edec70f6a0fc306f40b89265535fb) luminous
                                 (stable)",
                "cpu": "Intel(R) Xeon(R) CPU E5-2690 v3 @ 2.60GHz",
                "default_device_class": "hdd",
                "distro": "rhel",
                "distro_description": "Red Hat Enterprise Linux",
                "distro_version": "7.5",
                "filestore_backend": "xfs",
                "filestore_f_type": "0x58465342",
                "front_addr": "172.16.115.29:6800/1672",
                "front_iface": "eth0",
                "hb_back_addr": "172.16.115.29:6802/1672",
                "hb_front_addr": "172.16.115.29:6803/1672",
                "hostname": "ceph-shmohan-1537910194970-node2-osd",
                "journal_rotational": "1",
                "kernel_description": "#1 SMP Wed Mar 21 18:14:51 EDT 2018",
                "kernel_version": "3.10.0-862.el7.x86_64",
                "mem_swap_kb": "0",
                "mem_total_kb": "3880928",
                "os": "Linux",
                "osd_data": "/var/lib/ceph/osd/ceph-8",
                "osd_journal": "/var/lib/ceph/osd/ceph-8/journal",
                "osd_objectstore": "filestore",
                "rotational": "1"
             }

        """
        metadata_list = self.get_metadata_list("osd", client)
        for metadata in metadata_list:
            if metadata.get("id") == osd_id:
                return metadata

        return None

    def osd_check(self, client, cluster_name=None, rhbuild=None):
        """
        Check OSD status
        Args:
            client: client node to get OSD details
            rhbuild: ceph build version

        Returns:
            0 - Successful
            1 - failure
        """
        cmd = "ceph -s -f json"
        if cluster_name is not None:
            cmd += f" --cluster {cluster_name}"

        if rhbuild and rhbuild.split(".")[0] >= "5":
            cmd = f"cephadm shell -- {cmd}"

        out, err = client.exec_command(cmd=cmd, sudo=True)
        ceph_status_json = json.loads(out)

        # Support extraction of OSDmap attributes for 3.x, 4.x & 5.x
        osd_status = ceph_status_json["osdmap"].get(
            "osdmap", ceph_status_json["osdmap"]
        )
        num_osds = osd_status["num_osds"]
        num_up_osds = osd_status["num_up_osds"]
        num_in_osds = osd_status["num_in_osds"]
        if num_osds != num_up_osds:
            logger.error(
                "Not all osd's are up. Actual: %s / Expected: %s"
                % (num_up_osds, num_osds)
            )
            return 1

        if num_osds != num_in_osds:
            logger.error(
                "Not all osd's are in. Actual: %s / Expected: %s"
                % (num_in_osds, num_osds)
            )
            return 1

        if num_osds == num_up_osds == num_in_osds:
            logger.info("All osds are up and in")
            return 0

    def check_health(self, rhbuild, cluster_name=None, client=None, timeout=300):
        """
        Check if ceph is in healthy state

        Args:
           rhbuild (str): rhcs build version
           client(CephObject): ceph object with ceph-common and ceph-keyring
           timeout (int): max time to check if cluster is not healthy within timeout
                          period - return 1
        Returns:
           int: return 0 when ceph is in healthy state, else 1
        """
        pacific = True if (rhbuild and rhbuild.split(".")[0] >= "5") else False
        if not client:
            client = (
                self.get_ceph_object("client")
                if self.get_ceph_object("client")
                else self.get_ceph_object("mon")
            )

        timeout = datetime.timedelta(seconds=timeout)
        starttime = datetime.datetime.now()
        pending_states = ["peering", "activating", "creating"]
        valid_states = ["active+clean"]

        out = str()
        while datetime.datetime.now() - starttime <= timeout:
            cmd = "ceph -s"
            if cluster_name is not None:
                cmd += f" --cluster {cluster_name}"
            if pacific:
                cmd = f"cephadm shell -- {cmd}"

            out, _ = client.exec_command(cmd=cmd, sudo=True)

            if not any(state in out for state in pending_states):
                if all(state in out for state in valid_states):
                    break
            sleep(5)
        logger.info(out)
        if not all(state in out for state in valid_states):
            logger.error("Valid States are not found in the health check")
            return 1

        self.osd_check(client, rhbuild=rhbuild, cluster_name=cluster_name)

        # attempt luminous pattern first, if it returns none attempt jewel pattern
        if not pacific:
            cmd = "ceph quorum_status -f json"
            if cluster_name is not None:
                cmd += f" --cluster {cluster_name}"
            if pacific:
                cmd = f"cephadm shell -- {cmd}"

            out, _ = client.exec_command(cmd=cmd, sudo=True)
            mons = json.loads(out)
            logger.info(
                f"Expected MONS: {self.ceph_demon_stat['mon']}, MON quorum : {mons}"
            )

            if len(mons.get("quorum")) != self.ceph_demon_stat["mon"]:
                logger.error("Not all monitors are in cluster")
                return 1

        logger.info("Expected MONs is in quorum")

        if "HEALTH_ERR" in out:
            logger.error("HEALTH in ERROR STATE")
            return 1

        return 0

    def distribute_all_yml(self):
        """
        Distributes ansible all.yml config across all installers
        """
        gvar = yaml.dump(self.ansible_config, default_flow_style=False)
        for installer in self.get_ceph_objects("installer"):
            installer.append_to_all_yml(gvar)

        logger.info("updated all.yml: \n" + gvar)

    def refresh_ansible_config_from_all_yml(self, installer=None):
        """
        Refreshes ansible config based on installer all.yml content
        Args:
            installer(CephInstaller): Ceph installer. Will use first available installer
                                      if omitted
        """
        if not installer:
            installer = self.get_ceph_object("installer")

        self.ansible_config = installer.get_all_yml()

    def setup_packages(
        self,
        base_url,
        hotfix_repo,
        installer_url,
        ubuntu_repo,
        build=None,
        cloud_type="openstack",
        exclude_ansible=False,
    ):
        """
        Setup packages required for ceph-ansible istallation
        Args:
            base_url (str): rhel compose url
            hotfix_repo (str): hotfix repo to use with priority
            installer_url (str): installer url
            ubuntu_repo (str): deb repo url
            build (str):  ansible_config.build or rhbuild cli argument
            cloud_type (str): IaaS provider - defaults to OpenStack.
            exclude_ansible (bool): Excludes the ansible package from being upgraded
        """
        if not build:
            build = self.rhcs_version

        with parallel() as p:
            for node in self.get_nodes():
                if self.use_cdn:
                    if node.pkg_type == "deb":
                        if node.role == "installer":
                            logger.info("Enabling tools repository")
                            node.setup_deb_cdn_repos(build)
                    else:
                        logger.info("Using the cdn repo for the test")
                        distro_info = node.distro_info
                        distro_ver = distro_info["VERSION_ID"]
                        node.setup_rhceph_cdn_repos(build, distro_ver)
                else:
                    if (
                        self.ansible_config.get("ceph_repository_type") != "iso"
                        or self.ansible_config.get("ceph_repository_type") == "iso"
                        and (node.role == "installer")
                    ):
                        if node.pkg_type == "deb":
                            node.setup_deb_repos(ubuntu_repo)
                            sleep(15)
                            # install python2 on xenial
                            node.exec_command(
                                sudo=True, cmd="sudo apt-get install -y python"
                            )
                            node.exec_command(
                                sudo=True, cmd="apt-get install -y python-pip"
                            )
                            node.exec_command(sudo=True, cmd="apt-get install -y ntp")
                            node.exec_command(
                                sudo=True, cmd="apt-get install -y chrony"
                            )
                            node.exec_command(sudo=True, cmd="pip install nose")
                        else:
                            if hotfix_repo:
                                node.exec_command(
                                    sudo=True,
                                    cmd="wget -O /etc/yum.repos.d/rh_repo.repo {repo}".format(
                                        repo=hotfix_repo
                                    ),
                                )
                            else:
                                if (
                                    not self.ansible_config.get("ceph_repository_type")
                                    == "cdn"
                                ):
                                    node.setup_rhceph_repos(
                                        base_url, installer_url, cloud_type
                                    )
                    if (
                        self.ansible_config.get("ceph_repository_type") == "iso"
                        and node.role == "installer"
                    ):
                        iso_file_url = self.get_iso_file_url(base_url)
                        node.exec_command(
                            sudo=True, cmd="mkdir -p {}/iso".format(node.ansible_dir)
                        )
                        node.exec_command(
                            sudo=True,
                            cmd="wget -O {}/iso/ceph.iso {}".format(
                                node.ansible_dir, iso_file_url
                            ),
                        )
                if node.pkg_type == "rpm":
                    logger.info("Updating metadata")
                    node.exec_command(
                        sudo=True, cmd="yum update metadata", check_ec=False
                    )

                    cmd = "yum update -y"
                    if exclude_ansible:
                        cmd += " --exclude=ansible*"

                    p.spawn(
                        node.exec_command,
                        sudo=True,
                        cmd=cmd,
                        long_running=True,
                    )

                sleep(10)

    def create_rbd_pool(self, k_and_m, cluster_name=None):
        """
        Generate pools for later testing use
        Args:
            k_and_m(bool): ec-pool-k-m settings
        """
        ceph_mon = self.get_ceph_object("mon")

        if self.rhcs_version >= "3":
            if k_and_m:
                pool_name = "rbd"
                commands = [
                    f"ceph osd erasure-code-profile set ec_profile k={k_and_m[0]} m={k_and_m[2]}",
                    f"ceph osd pool create {pool_name} 64 64 erasure ec_profile",
                    f"ceph osd pool set {pool_name} allow_ec_overwrites true",
                    f"ceph osd pool application enable {pool_name} rbd --yes-i-really-mean-it",
                ]

            else:
                commands = [
                    "ceph osd pool create rbd 64 64",
                    "ceph osd pool application enable rbd rbd --yes-i-really-mean-it",
                ]
            if cluster_name is not None:
                commands = [
                    f"{command} --cluster {cluster_name}" for command in commands
                ]
            for command in commands:
                ceph_mon.exec_command(sudo=True, cmd=command)

    @staticmethod
    def get_iso_file_url(base_url):
        """
        Returns iso url for given compose link

        Args:
            base_url(str): rhel compose

        Returns:
            str:  iso file url
        """
        iso_file_path = base_url + "compose/Tools/x86_64/iso/"
        iso_dir_html = requests.get(iso_file_path, timeout=10, verify=False).content
        match = re.search('<a href="(.*?)">(.*?)-x86_64-dvd.iso</a>', iso_dir_html)
        iso_file_name = match.group(1)
        logger.info("Using {}".format(iso_file_name))
        iso_file = iso_file_path + iso_file_name
        return iso_file

    @staticmethod
    def generate_repository_file(base_url, repos, cloud_type="openstack"):
        """
        Generate rhel repository file for given repos.

        Args:
            base_url(str): rhel compose url
            repos(list): repos behind compose/ to process
            cloud_type (str): The environment used for testing
        Returns:
            str: repository file content
        """
        repo_file = ""
        for repo in repos:
            base_url = base_url.rstrip("/")
            if "ibmc" in cloud_type:
                repo_to_use = f"{base_url}/{repo}/"
            else:
                repo_to_use = f"{base_url}/compose/{repo}/x86_64/os/"

            logger.info(f"repo to use is {repo_to_use}")
            r = requests.get(repo_to_use, timeout=10, verify=False)
            logger.info("Checking %s", repo_to_use)
            if r.status_code == 200:
                logger.info("Using %s", repo_to_use)
                header = "[ceph-" + repo + "]" + "\n"
                name = "name=ceph-" + repo + "\n"
                baseurl = "baseurl=" + repo_to_use + "\n"
                gpgcheck = "gpgcheck=0\n"
                enabled = "enabled=1\n\n"
                repo_file = repo_file + header + name + baseurl + gpgcheck + enabled

        return repo_file

    def get_osd_container_name_by_id(self, osd_id, client=None):
        """
        Args:
            osd_id:
            client:

        Returns:

        """
        return self.get_osd_by_id(osd_id, client).container_name

    def get_osd_by_id(self, osd_id, client=None):
        """

        Args:
            osd_id:
            client:

        Returns:
            CephDemon:

        """
        hostname = self.get_osd_metadata(osd_id).get("hostname")
        node = self.get_node_by_hostname(hostname)
        osd_device = self.get_osd_device(osd_id)
        osd_demon_list = [
            osd_demon
            for osd_demon in node.get_ceph_objects("osd")
            if osd_demon.device == osd_device
        ]
        return osd_demon_list[0] if len(osd_demon_list) > 0 else None

    @staticmethod
    def get_osd_service_name(osd_id, client=None):
        """
        Return the service name of the OSD daemon.

        Args:
            osd_id:
            client:

        Returns:

        """
        osd_service_id = osd_id
        osd_service_name = "ceph-osd@{id}".format(id=osd_service_id)
        return osd_service_name

    def get_osd_device(self, osd_id, client=None):
        """

        Args:
            osd_id:
            client:

        Returns:

        """
        osd_metadata = self.get_osd_metadata(osd_id, client)
        if osd_metadata.get("osd_objectstore") == "filestore":
            osd_device = osd_metadata.get("backend_filestore_dev_node")
        elif osd_metadata.get("osd_objectstore") == "bluestore":
            osd_device = osd_metadata.get("bluefs_db_dev_node")
        else:
            raise RuntimeError(
                "Unable to detect filestore type for osd #{osd_id}".format(
                    osd_id=osd_id
                )
            )
        return osd_device

    def get_node_by_hostname(self, hostname):
        """
        Returns Ceph node by it's hostname
        Args:
            hostname (str): hostname
        """
        node_list = [node for node in self.get_nodes() if node.hostname == hostname]
        return node_list[0] if len(node_list) > 0 else None

    def get_osd_data_partition_path(self, osd_id, client=None):
        """
        Returns data partition path by given osd id
        Args:
            osd_id (int): osd id
            client (CephObject): client, optional

        Returns:
            str: data partition path

        """
        osd_metadata = self.get_osd_metadata(osd_id, client)
        osd_data = osd_metadata.get("osd_data")
        osd_object = self.get_osd_by_id(osd_id, client)
        out, err = osd_object.exec_command(
            "ceph-volume simple scan {osd_data} --stdout".format(osd_data=osd_data),
            check_ec=False,
        )
        simple_scan = json.loads(out[out.index("{") : :])
        return simple_scan.get("data").get("path")

    def get_osd_data_partition(self, osd_id, client=None):
        """
        Returns data partition by given osd id
        Args:
            osd_id (int): osd id
            client (CephObject): client, optional

        Returns:
            str: data path
        """
        osd_partition_path = self.get_osd_data_partition_path(osd_id, client)
        return osd_partition_path[osd_partition_path.rfind("/") + 1 : :]

    def get_public_networks(self) -> str:
        """Returns a comma separated list of public networks."""
        if not self.networks:
            return ""

        return ",".join(self.networks.get("public", []))

    def get_cluster_networks(self) -> str:
        """Returns a comma separated list of cluster networks."""
        if not self.networks:
            return ""

        return ",".join(self.networks.get("cluster", []))

    def get_nodes_in_location(self, location: str) -> list:
        """Return the list of nodes found in the location."""
        return [node for node in self.node_list if node.vm_node.location == location]

    def get_cluster_fsid(self, rhbuild, cluster_name=None, client=None):
        """
            Fetch the fsid of the cluster
        Args:
            rhbuild: rhbuild value for the ceph cluster
            cluster_name: name of the cluster
            client: client node

        Returns:
            Returns the fsid of the cluster
        """
        cmd = "ceph fsid"
        pacific = True if (rhbuild and rhbuild.split(".")[0] >= "5") else False
        if not client:
            client = (
                self.get_ceph_object("client")
                if self.get_ceph_object("client")
                else self.get_ceph_object("mon")
            )
        if cluster_name is not None:
            cmd += f" --cluster {cluster_name}"
        if pacific and not self.get_ceph_object("client"):
            cmd = f"cephadm shell -- {cmd}"

        out, _ = client.exec_command(cmd=cmd, sudo=True)
        return out.strip()

    def get_mgr_services(self):
        """Fetch `mgr` services"""
        # Set json format & get client node
        args, node = {"format": "json"}, self.get_ceph_object("client")

        # Check for mandatory client node
        if not node:
            raise ResourceNotFoundError("Client node not provided")

        # Return json data
        return json.loads(CephCli(node).mgr.services(**args)[0])


class CommandFailed(Exception):
    pass


class TimeoutException(Exception):
    """Operation timeout exception."""

    pass


def check_timeout(end_time, timeout):
    """Raises an exception when current time is greater"""
    if timeout and datetime.datetime.now() >= end_time:
        raise TimeoutException("Command exceed the allocated execution time.")


def read_stream(channel, end_time, timeout, stderr=False, log=True):
    """Reads the data from the given channel.

    Args:
      channel: the paramiko.Channel object to be used for reading.
      end_time: maximum allocated time for reading from the channel.
      timeout: Flag to check if timeout must be enforced.
      stderr: read from the stderr stream. Default is False.
      log: log the output. Default is True.

    Returns:
      a string with the data read from the channel.

    Raises:
      TimeoutException: if reading from the channel exceeds the allocated time.
    """
    _output = bytearray()
    _stream = channel.recv_stderr if stderr else channel.recv
    _decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    _data = _stream(2048)
    while _data:
        _output.extend(_data)
        if log:
            for _ln in _data.splitlines():
                _log = logger.error if stderr else logger.debug
                _log(_decoder.decode(_ln, final=False))

        check_timeout(end_time, timeout)
        _data = _stream(2048)

    try:
        return _decoder.decode(_output, final=True)  # Final decode
    except UnicodeDecodeError as e:
        logger.error(f"Decoding failed: {e}. Replacing invalid characters.")
        return _output.decode("utf-8", errors="replace")  # Fallback to safe decode


class RolesContainer(object):
    """
    Container for single or multiple node roles.

    Can be used as iterable or with equality '==' operator to check if role is present
    for the node. Note that '==' operator will behave the same way as 'in' operator
    i.e. check that value is present in the role list.
    """

    def __init__(self, role="pool"):
        if isinstance(role, str):
            self.role_list = [str(role)]
        else:
            self.role_list = role if len(role) > 0 else ["pool"]

    def __eq__(self, role):
        if isinstance(role, str):
            return role in self.role_list
        else:
            return all(atomic_role in role for atomic_role in self.role_list)

    def __ne__(self, role):
        return not self.__eq__(role)

    def equals(self, other):
        if getattr(other, "role_list") == self.role_list:
            return True
        else:
            return False

    def __len__(self):
        return len(self.role_list)

    def __getitem__(self, key):
        return self.role_list[key]

    def __setitem__(self, key, value):
        self.role_list[key] = value

    def __delitem__(self, key):
        del self.role_list[key]

    def __iter__(self):
        return iter(self.role_list)

    def remove(self, object):
        self.role_list.remove(object)

    def append(self, object):
        self.role_list.append(object)

    def extend(self, iterable):
        self.role_list.extend(iterable)
        self.role_list = list(set(self.role_list))

    def update_role(self, roles_list):
        if "pool" in self.role_list:
            self.role_list.remove("pool")
        self.extend(roles_list)

    def clear(self):
        self.role_list = ["pool"]


class NodeVolume(object):
    FREE = "free"
    ALLOCATED = "allocated"

    def __init__(self, status, path=None):
        self.status = status
        self.path = path


class SSHConnectionManager(object):
    def __init__(
        self,
        ip_address,
        username,
        password,
        look_for_keys=False,
        private_key_file_path="",
        outage_timeout=600,
    ):
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.look_for_keys = look_for_keys
        self.pkey = self._get_ssh_key(private_key_file_path) if look_for_keys else None
        self.__client = paramiko.SSHClient()
        self.__client.set_missing_host_key_policy(paramiko.MissingHostKeyPolicy())
        self.__transport = None
        self.__outage_start_time = None
        self.outage_timeout = datetime.timedelta(seconds=outage_timeout)

    @property
    def client(self):
        return self.get_client()

    def _get_ssh_key(self, private_key_file_path):
        """Get SSH key based on file type"""
        private_key = None
        with open(private_key_file_path, "rb") as key_file:
            key_data = key_file.read()

        try:
            # Try loading as OpenSSH first
            private_key = (
                cryptography.hazmat.primitives.serialization.load_ssh_private_key(
                    key_data,
                    password=None,
                    backend=cryptography.hazmat.backends.default_backend(),
                )
            )
        except ValueError:
            # Fall back to PEM format
            private_key = (
                cryptography.hazmat.primitives.serialization.load_pem_private_key(
                    key_data,
                    password=None,
                    backend=cryptography.hazmat.backends.default_backend(),
                )
            )

        if isinstance(
            private_key,
            cryptography.hazmat.primitives.asymmetric.rsa.RSAPrivateKey,
        ):
            return paramiko.RSAKey.from_private_key_file(private_key_file_path)

        elif isinstance(
            private_key,
            cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey,
        ):
            return paramiko.Ed25519Key.from_private_key_file(private_key_file_path)

        logger.error("Unsupported ssh key {}".format(private_key_file_path))
        return False

    def get_client(self):
        if not (self.__transport and self.__transport.is_active()):
            self.__connect()
            self.__transport = self.__client.get_transport()

        return self.__client

    def __connect(self):
        """Establishes a connection with the remote host using the IP Address."""
        end_time = datetime.datetime.now() + self.outage_timeout
        while end_time > datetime.datetime.now():
            try:
                self.__client.connect(
                    self.ip_address,
                    username=self.username,
                    password=self.password,
                    look_for_keys=self.look_for_keys,
                    allow_agent=False,
                    pkey=self.pkey,
                )
                self.__outage_start_time = None
                return
            except Exception as e:
                logger.warning(f"Error in connecting to {self.ip_address}: \n{e}")
                if not self.__outage_start_time:
                    self.__outage_start_time = datetime.datetime.now()

                logger.debug("Retrying connection in 10 seconds")
                sleep(10)

        raise AssertionError(f"Unable to establish a connection with {self.ip_address}")

    @property
    def transport(self):
        return self.get_transport()

    def get_transport(self):
        self.__transport = self.client.get_transport()
        return self.__transport

    def __getstate__(self):
        pickle_dict = self.__dict__.copy()
        if pickle_dict.get("_SSHConnectionManager__transport"):
            del pickle_dict["_SSHConnectionManager__transport"]
        if pickle_dict.get("_SSHConnectionManager__client"):
            del pickle_dict["_SSHConnectionManager__client"]
        return pickle_dict


class CephNode(object):
    class LvmConfig(object):
        vg_name = "vg%s"
        lv_name = "lv%s"
        size = "{}%FREE"
        data_lv = "data-lv%s"
        db_lv = "db-lv%s"
        wal_lv = "wal-lv%s"

    def __init__(self, **kw):
        """
        Initialize a CephNode in a libcloud environment
        eg CephNode(username='cephuser', password='cephpasswd',
                    root_password='passwd', ip_address='ip_address',
                    subnet='subnet', hostname='hostname',
                    role='mon|osd|client', no_of_volumes=3,
                    ceph_vmnode='ref_to_libcloudvm')

        """
        self.username = kw["username"]
        self.password = kw["password"]
        self.root_passwd = kw["root_password"]
        self.look_for_key = kw["look_for_key"]
        self.private_key_path = kw["private_key_path"]
        self.root_login = kw["root_login"]
        self.private_ip = kw["private_ip"]
        self.ip_address = kw["ip_address"]
        self.subnet = kw["subnet"]
        self.vmname = kw["hostname"]
        self.ceph_nodename = kw["ceph_nodename"]
        self.vmshortname = self.vmname.split(".")[0]

        if kw.get("ceph_vmnode"):
            self.vm_node = kw["ceph_vmnode"]
            self.osd_scenario = self.vm_node.osd_scenario
        self.id = kw.get("id", None)

        self.volume_list = []
        if kw["no_of_volumes"]:
            if self.vm_node.node_type == "baremetal":
                self.volume_list = [
                    NodeVolume(NodeVolume.FREE, vol) for vol in self.vm_node.volumes
                ]
            else:
                self.volume_list = [
                    NodeVolume(NodeVolume.FREE) for _ in range(kw["no_of_volumes"])
                ]

        self.ceph_object_list = [
            CephObjectFactory(self).create_ceph_object(role)
            for role in kw["role"]
            if role != "pool"
        ]
        while (
            len(self.get_ceph_objects("osd")) > 0 and len(self.get_free_volumes()) > 0
        ):
            self.ceph_object_list.append(
                CephObjectFactory(self).create_ceph_object("osd")
            )

        self.root_connection = SSHConnectionManager(
            self.ip_address,
            "root",
            self.root_passwd,
            look_for_keys=self.look_for_key,
            private_key_file_path=self.private_key_path,
        )
        self.connection = SSHConnectionManager(
            self.ip_address,
            self.username,
            self.password,
            look_for_keys=self.look_for_key,
            private_key_file_path=self.private_key_path,
        )
        self.rssh = self.root_connection.get_client
        self.rssh_transport = self.root_connection.get_transport
        self.ssh = self.connection.get_client
        self.ssh_transport = self.connection.get_transport
        self.run_once = False

    @property
    def distro_info(self):
        out, err = self.exec_command(cmd="cat /etc/os-release")
        info = out.split("\n")
        info = filter(None, info)
        info_dict = {}
        for each in info:
            key, value = each.rstrip().split("=")
            info_dict[key] = value.strip('"')
        return info_dict

    @property
    def role(self):
        return RolesContainer(
            [ceph_demon.role for ceph_demon in self.ceph_object_list if ceph_demon]
        )

    def get_free_volumes(self):
        return [
            volume for volume in self.volume_list if volume.status == NodeVolume.FREE
        ]

    def get_allocated_volumes(self):
        return [
            volume
            for volume in self.volume_list
            if volume.status == NodeVolume.ALLOCATED
        ]

    def get_ceph_demons(self, role=None):
        """
        Get Ceph demons list.

        Only active (those which will be part of the cluster) demons are shown.

        Returns:
            list: list of CephDemon

        """
        return [
            ceph_demon
            for ceph_demon in self.get_ceph_objects(role)
            if isinstance(ceph_demon, CephDemon) and ceph_demon.is_active
        ]

    def connect(self):
        """
        connect to ceph instance using paramiko ssh protocol
        eg: self.connect()
        - setup tcp keepalive to max retries for active connection
        - set up hostname and shortname as attributes for tests to query
        """
        logger.info(
            "Connecting {host_name} / {ip_address}".format(
                host_name=self.vmname, ip_address=self.ip_address
            )
        )

        self.rssh().exec_command("dmesg")
        self.rssh_transport().set_keepalive(15)
        _, stdout, stderr = self.rssh().exec_command(
            f"echo '{self.username}:{self.password}' | chpasswd"
        )
        logger.info(stdout.readlines())
        _, stdout, stderr = self.rssh().exec_command(
            f"echo 'root:{self.root_passwd}' | chpasswd"
        )
        logger.info(stdout.readlines())
        self.rssh().exec_command("echo 120 > /proc/sys/net/ipv4/tcp_keepalive_time")
        self.rssh().exec_command("echo 60 > /proc/sys/net/ipv4/tcp_keepalive_intvl")
        self.rssh().exec_command("echo 20 > /proc/sys/net/ipv4/tcp_keepalive_probes")
        self.exec_command(cmd="ls / ; uptime ; date")
        self.ssh_transport().set_keepalive(15)
        if self.vm_node.node_type == "baremetal":
            out, err = self.exec_command(cmd="hostname -s")
        else:
            out, err = self.exec_command(cmd="hostname")
        self.hostname = out.strip()

        shortname = self.hostname.split(".")
        self.shortname = shortname[0]
        logger.info(
            "hostname and shortname set to %s and %s", self.hostname, self.shortname
        )
        self.set_internal_ip()
        self.exec_command(cmd="echo 'TMOUT=600' >> ~/.bashrc")
        self.exec_command(cmd="[ -f /etc/redhat-release ]", check_ec=False)

        if self.exit_status == 0:
            self.pkg_type = "rpm"
        else:
            self.pkg_type = "deb"

        logger.info("finished connect")
        self.run_once = True

    def set_internal_ip(self):
        """
        set the internal ip of the vm which differs from floating ip
        """
        out, _ = self.exec_command(
            cmd="/sbin/ifconfig eth0 | grep 'inet ' | awk '{ print $2}'"
        )
        self.internal_ip = out.strip()

    def set_eth_interface(self, eth_interface):
        """
        set the eth interface
        """
        self.eth_interface = eth_interface

    def generate_id_rsa(self):
        """
        generate id_rsa key files for the new vm node
        """
        # remove any old files
        self.exec_command(
            cmd="test -f ~/.ssh/id_rsa.pub && rm -f ~/.ssh/id*", check_ec=False
        )
        self.exec_command(cmd="ssh-keygen -b 2048 -f ~/.ssh/id_rsa -t rsa -q -N ''")
        self.id_rsa_pub, _ = self.exec_command(cmd="cat ~/.ssh/id_rsa.pub")
        out, rc = self.exec_command(cmd="sudo hostname")
        logger.info(out)
        self.exec_command(cmd="sudo hostnamectl set-hostname $(hostname -s)")

    def long_running(self, **kw):
        """Method to execute long-running command.

        Args:
            **kw: execute command configuration

        Returns:
            ec: exit status
        """
        cmd = kw["cmd"]
        _end_time = None
        _verbose = kw.get("verbose", False)
        ssh = self.rssh if kw.get("sudo") else self.ssh
        long_running = kw.get("long_running", False)
        if "timeout" in kw:
            timeout = None if kw["timeout"] == "notimeout" else kw["timeout"]
        else:
            # Set defaults if long_running then 1h else 5m
            timeout = 3600 if kw.get("long_running", False) else 600

        try:
            channel = ssh().get_transport().open_session(timeout=timeout)
            channel.settimeout(timeout)

            logger.info("Execute %s on %s", cmd, self.ip_address)
            _exec_start_time = datetime.datetime.now()
            channel.exec_command(cmd)

            if timeout:
                _end_time = datetime.datetime.now() + datetime.timedelta(
                    seconds=timeout
                )

            _out = ""
            _err = ""
            while not channel.exit_status_ready():
                # Prevent high resource consumption
                sleep(1)

                # Check the streams for data and log in debug mode only if it
                # is a long running command else don't log.
                # Fixme: logging must happen in debug irrespective of type.
                _verbose = True if long_running else _verbose
                if channel.recv_ready():
                    _out += read_stream(channel, _end_time, timeout, log=_verbose)

                if channel.recv_stderr_ready():
                    _err += read_stream(
                        channel, _end_time, timeout, stderr=True, log=_verbose
                    )

                check_timeout(_end_time, timeout)

            _time = (datetime.datetime.now() - _exec_start_time).total_seconds()
            logger.info(
                "Execution of %s on %s took %s seconds",
                cmd,
                self.ip_address,
                str(_time),
            )

            # Check for data residues in the channel streams. This is required for the following reasons
            #   - exit_ready and first line is blank causing data to be None
            #   - race condition between data read and exit ready
            try:
                _new_timeout = datetime.datetime.now() + datetime.timedelta(seconds=10)
                _out += read_stream(channel, _new_timeout, timeout=True)
                _err += read_stream(channel, _new_timeout, timeout=True, stderr=True)
            except CommandFailed:
                logger.debug("Encountered a timeout during read post execution.")
            except BaseException as be:
                logger.debug("Encountered an unknown exception during last read.\n", be)

            _exit = channel.recv_exit_status()
            return _out, _err, _exit, _time
        except socket.timeout as terr:
            logger.error("%s failed to execute within %d seconds.", cmd, timeout)
            raise SocketTimeoutException(terr)
        except TimeoutException as tex:
            channel.close()
            logger.error("%s failed to execute within %ds.", cmd, timeout)
            raise CommandFailed(tex)
        except BaseException as be:  # noqa
            logger.exception(be)
            raise CommandFailed(be)

    def exec_command(self, **kw):
        """Execute the given command on the remote host.

        Args:
          cmd: The command that needs to be executed on the remote host.
          long_running: Bool flag to indicate if the command is long running.
          check_ec: Bool flag to indicate if the command should check for error code.
          timeout: Max time to wait for command to complete. Default is 600 seconds.
          pretty_print: Bool flag to indicate if the output should be pretty printed.
          verbose: Bool flag to indicate if the command output should be printed.

        Returns:
          Exit code when long_running is used
          Tuple having stdout, stderr data output when long_running is not used
          Tupe having stdout, stderr, exit code, duration when verbose is enabled

        Raises:
          CommandFailed: when the exit code is non-zero and check_ec is enabled.
          TimeoutError: when the command times out.

        Examples:
            self.exec_cmd(cmd='uptime')
          or
            self.exec_cmd(cmd='background_cmd', check_ec=False)
        """
        if self.run_once:
            self.ssh_transport().set_keepalive(15)
            self.rssh_transport().set_keepalive(15)

        cmd = kw["cmd"]
        _out, _err, _exit, _time = self.long_running(**kw)
        self.exit_status = _exit

        if kw.get("pretty_print"):
            msg = f"\nCommand:    {cmd}"
            msg += f"\nDuration:   {_time} seconds"
            msg += f"\nExit Code:  {_exit}"

            if _out:
                msg += f"\nStdout:     {_out}"

            if _err:
                msg += f"\nStderr:      {_err}"

            logger.info(msg)

        if "verbose" in kw:
            return _out, _err, _exit, _time

        # Historically, we are only providing command exit code for long
        # running commands.
        # Fixme: Ensure the method returns a tuple of
        #        (stdout, stderr, exit_code, time_taken)
        if kw.get("long_running", False):
            if kw.get("check_ec", False) and _exit != 0:
                raise CommandFailed(
                    f"{cmd} returned {_err} and code {_exit} on {self.ip_address}"
                )

            return _exit

        if kw.get("check_ec", True) and _exit != 0:
            raise CommandFailed(
                f"{cmd} returned {_err} and code {_exit} on {self.ip_address}"
            )

        return _out, _err

    def remote_file(self, **kw):
        """Return contents of the remote file."""
        client = self.rssh if kw.get("sudo", False) else self.ssh
        file_name = kw["file_name"]
        file_mode = kw["file_mode"]
        ftp = client().open_sftp()
        remote_file = ftp.file(file_name, file_mode, -1)

        return remote_file

    def _keep_alive(self):
        while True:
            self.exec_command(cmd="uptime", check_ec=False)
            sleep(60)

    def reconnect(self):
        """Re-establish the connections."""
        logger.info(f"Re-establishing the connection to {self.ip_address}.")
        self.root_connection.get_client()
        self.connection.get_client()

    def __getstate__(self):
        d = dict(self.__dict__)

        if d.get("rssh"):
            del d["rssh"]

        if d.get("ssh"):
            del d["ssh"]

        if d.get("rssh_transport"):
            del d["rssh_transport"]

        if d.get("ssh_transport"):
            del d["ssh_transport"]

        if d.get("ssh_transport"):
            del d["root_connection"]

        if d.get("connection"):
            del d["connection"]

        return d

    def __setstate__(self, pickle_dict):
        self.__dict__.update(pickle_dict)
        self.root_connection = SSHConnectionManager(
            self.ip_address,
            "root",
            self.root_passwd,
            look_for_keys=self.look_for_key,
            private_key_file_path=self.private_key_path,
        )
        self.connection = SSHConnectionManager(
            self.ip_address,
            self.username,
            self.password,
            look_for_keys=self.look_for_key,
            private_key_file_path=self.private_key_path,
        )
        self.rssh = self.root_connection.get_client
        self.ssh = self.connection.get_client
        self.rssh_transport = self.root_connection.get_transport
        self.ssh_transport = self.connection.get_transport

    def get_ceph_objects(self, role=None):
        """
        Get Ceph objects list on the node
        Args:
            role(str): Ceph object role

        Returns:
            list: ceph objects

        """
        return [
            ceph_demon
            for ceph_demon in self.ceph_object_list
            if ceph_demon.role == role or not role
        ]

    def create_ceph_object(self, role):
        """
        Create ceph object on the node
        Args:
            role(str): ceph object role

        Returns:
            CephObject|CephDemon: created ceph object
        """
        ceph_object = CephObjectFactory(self).create_ceph_object(role)
        self.ceph_object_list.append(ceph_object)
        return ceph_object

    def remove_ceph_object(self, ceph_object):
        """
        Removes ceph object form the node
        Args:
            ceph_object(CephObject): ceph object to remove
        """
        self.ceph_object_list.remove(ceph_object)
        if ceph_object.role == "osd":
            self.get_allocated_volumes()[0].status = NodeVolume.FREE

    def configure_firewall(self):
        """Configures firewall based on the package manager"""
        if self.pkg_type == "rpm":
            try:
                self.exec_command(sudo=True, cmd="rpm -qa | grep firewalld")
            except CommandFailed:
                self.exec_command(
                    sudo=True, cmd="yum install -y firewalld", long_running=True
                )
            self.exec_command(sudo=True, cmd="systemctl enable firewalld")
            self.exec_command(sudo=True, cmd="systemctl start firewalld")
            self.exec_command(sudo=True, cmd="systemctl status firewalld")
        elif self.pkg_type == "deb":
            # Ubuntu section stub
            pass

    def open_firewall_port(self, port, protocol):
        """
        Opens firewall port on the node

        Args:
            port(str|int|list): port(s) to be enabled
            protocol(str): protocol
        """
        if self.pkg_type == "rpm":
            cmd = "firewall-cmd --zone=public "

            if isinstance(port, (str, int)):
                cmd += f"--add-port={port}/{protocol}"
            elif isinstance(port, list):
                cmd += " ".join([f"--add-port={p}/{protocol}" for p in port])
            else:
                pass

            self.exec_command(sudo=True, cmd=cmd)
            self.exec_command(sudo=True, cmd=f"{cmd} --permanent")

        elif self.pkg_type == "deb":
            # Ubuntu section stub
            pass

    def search_ethernet_interface(self, ceph_node_list):
        """
        Search interface on the given node which allows every node in the cluster
        accesible by it's shortname.

        Args:
            ceph_node_list (list): lsit of CephNode

        Returns:
            eth_interface (str): retturns None if no suitable interface found

        """
        logger.info(
            "Searching suitable ethernet interface on {node}".format(
                node=self.ip_address
            )
        )
        out, err = self.exec_command(cmd="sudo ls /sys/class/net | grep -v lo")
        eth_interface_list = out.strip().split("\n")
        for eth_interface in eth_interface_list:
            try:
                for ceph_node in ceph_node_list:
                    if self.vmname == ceph_node.vmname:
                        logger.info("Skipping ping check on localhost")
                        continue
                    self.exec_command(
                        cmd="sudo ping -I {interface} -c 3 {ceph_node}".format(
                            interface=eth_interface, ceph_node=ceph_node.shortname
                        )
                    )
                logger.info(
                    "Suitable ethernet interface {eth_interface} found on {node}".format(
                        eth_interface=eth_interface, node=ceph_node.ip_address
                    )
                )
                return eth_interface
            except Exception:  # no-qa
                continue

        logger.info(
            "No suitable ethernet interface found on {node}".format(
                node=ceph_node.ip_address
            )
        )

        return None

    def setup_deb_cdn_repos(self, build):
        """
        Setup cdn repositories for deb systems
        Args:
            build(str|LooseVersion): rhcs version
        """
        user = "redhat"
        passwd = "OgYZNpkj6jZAIF20XFZW0gnnwYBjYcmt7PeY76bLHec9"
        num = str(build).split(".")[0]
        cmd = (
            "umask 0077; echo deb https://{user}:{passwd}@rhcs.download.redhat.com/{num}-updates/Tools "
            "$(lsb_release -sc) main | tee /etc/apt/sources.list.d/Tools.list".format(
                user=user, passwd=passwd, num=num
            )
        )
        self.exec_command(sudo=True, cmd=cmd)
        self.exec_command(
            sudo=True,
            cmd="wget -O - https://www.redhat.com/security/fd431d51.txt | apt-key add -",
        )
        self.exec_command(sudo=True, cmd="apt-get update")

    def setup_rhceph_cdn_repos(self, build, distro_ver):
        """
        Setup cdn repositories for rhel systems
        Args:
            build(str): rhcs version
            distro_ver: os distro version from /etc/os-release
        """
        repos_13x = [
            "rhel-7-server-rhceph-1.3-mon-rpms",
            "rhel-7-server-rhceph-1.3-osd-rpms",
            "rhel-7-server-rhceph-1.3-calamari-rpms",
            "rhel-7-server-rhceph-1.3-installer-rpms",
            "rhel-7-server-rhceph-1.3-tools-rpms",
        ]

        repos_2x = [
            "rhel-7-server-rhceph-2-mon-rpms",
            "rhel-7-server-rhceph-2-osd-rpms",
            "rhel-7-server-rhceph-2-tools-rpms",
            "rhel-7-server-rhscon-2-agent-rpms",
            "rhel-7-server-rhscon-2-installer-rpms",
            "rhel-7-server-rhscon-2-main-rpms",
        ]

        repos_3x = [
            "rhel-7-server-rhceph-3-tools-rpms",
            "rhel-7-server-rhceph-3-osd-rpms",
            "rhel-7-server-rhceph-3-mon-rpms",
        ]

        repos_4x_rhel7 = [
            "rhel-7-server-rhceph-4-tools-rpms",
            "rhel-7-server-rhceph-4-osd-rpms",
            "rhel-7-server-rhceph-4-mon-rpms",
        ]

        repos_4x_rhel8 = [
            "rhceph-4-tools-for-rhel-8-x86_64-rpms",
            "rhceph-4-mon-for-rhel-8-x86_64-rpms",
            "rhceph-4-osd-for-rhel-8-x86_64-rpms",
        ]

        repos_5x = ["rhceph-5-tools-for-rhel-8-x86_64-rpms"]

        repos = None
        if build.startswith("1"):
            repos = repos_13x
        elif build.startswith("2"):
            repos = repos_2x
        elif build.startswith("3"):
            repos = repos_3x
        elif build.startswith("4") & distro_ver.startswith("8"):
            repos = repos_4x_rhel8
        elif build.startswith("4") & distro_ver.startswith("7"):
            repos = repos_4x_rhel7
        elif build.startswith("5"):
            repos = repos_5x

        self.exec_command(
            sudo=True, cmd="subscription-manager repos --enable={r}".format(r=repos[0])
        )
        # ansible will enable remaining osd/mon rpms

    def setup_deb_repos(self, deb_repo):
        """
        Setup repositories for deb system
        Args:
            deb_repo(str): deb (Ubuntu) repository link
        """
        self.exec_command(cmd="sudo rm -f /etc/apt/sources.list.d/*")
        repos = ["MON", "OSD", "Tools"]
        for repo in repos:
            cmd = (
                "sudo echo deb "
                + deb_repo
                + "/{0}".format(repo)
                + " $(lsb_release -sc) main"
            )
            self.exec_command(cmd=cmd + " > " + "/tmp/{0}.list".format(repo))
            self.exec_command(
                cmd="sudo cp /tmp/{0}.list".format(repo) + " /etc/apt/sources.list.d/"
            )
        ds_keys = [
            "https://www.redhat.com/security/897da07a.txt",
            "https://www.redhat.com/security/f21541eb.txt",
            "https://prodsec.redhat.com/keys/00da75f2.txt",
            "http://file.corp.redhat.com/~kdreyer/keys/00da75f2.txt",
            "https://www.redhat.com/security/data/fd431d51.txt",
        ]

        for key in ds_keys:
            wget_cmd = "sudo wget -O - " + key + " | sudo apt-key add -"
            self.exec_command(cmd=wget_cmd)
            self.exec_command(cmd="sudo apt-get update")

    def setup_rhceph_repos(self, base_url, installer_url=None, cloud_type="openstack"):
        """
        Setup repositories for rhel
        Args:
            base_url (str): compose url for rhel
            installer_url (str): installer repos url
        """
        if base_url.endswith(".repo"):
            cmd = f"yum-config-manager --add-repo {base_url}"
            self.exec_command(sudo=True, cmd=cmd)

        else:
            repos = ["MON", "OSD", "Tools", "Calamari", "Installer"]
            base_repo = Ceph.generate_repository_file(base_url, repos, cloud_type)
            base_file = self.remote_file(
                sudo=True, file_name="/etc/yum.repos.d/rh_ceph.repo", file_mode="w"
            )
            base_file.write(base_repo)
            base_file.flush()

        if installer_url is not None:
            installer_repos = ["Agent", "Main", "Installer"]
            inst_repo = Ceph.generate_repository_file(
                installer_url, installer_repos, cloud_type
            )
            logger.info("Setting up repo on %s", self.hostname)
            inst_file = self.remote_file(
                sudo=True, file_name="/etc/yum.repos.d/rh_ceph_inst.repo", file_mode="w"
            )
            inst_file.write(inst_repo)
            inst_file.flush()

    def obtain_root_permissions(self, path):
        """
        Transfer ownership of root to current user for the path given. Recursive.
        Args:
            path(str): file path
        """
        self.exec_command(cmd="sudo chown -R $USER:$USER {path}".format(path=path))

    def create_lvm(self, devices, num=None, check_lvm=True):
        """
        Creates lvm volumes and returns device list suitable for ansible config
        Args:
            devices: list of devices
            num: number to concatenate with pv,vg and lv names
            check_lvm: To check if lvm exists is optional, by default checking is enabled

        Returns (list): lvm volumes list

        """
        self.install_lvm_util()
        lvm_volms = []
        file_Name = "osd_scenarios_%s"
        exists = self.chk_lvm_exists() if check_lvm else 1
        if exists == 0:
            """
            for script test_ansible_roll_over.py, which adds new OSD,
            to prevent creation of lvms on the existing osd, using this chk_lvm_exists()

            """
            logger.info("lvms configured already ")
            fileObject = open(file_Name % self.hostname, "rb")
            existing_osd_scenarios = pickle.load(fileObject)
            lvm_volms.append(existing_osd_scenarios)
            fileObject.close()
        else:
            for dev in devices:
                number = devices.index(dev) if not num else num
                logger.info("creating pv on %s" % self.hostname)
                lvm_utils.pvcreate(self, dev)
                logger.info("creating vg  %s" % self.hostname)
                vgname = lvm_utils.vgcreate(self, self.LvmConfig.vg_name % number, dev)
                logger.info("creating lv %s" % self.hostname)
                lvname = lvm_utils.lvcreate(
                    self,
                    self.LvmConfig.lv_name % number,
                    self.LvmConfig.vg_name % number,
                    self.LvmConfig.size.format(100),
                )
                lvm_volms.append({"data": lvname, "data_vg": vgname})

        if check_lvm:
            fileObject = open(file_Name % self.hostname, "wb")
            pickle.dump(lvm_volms, fileObject)
            fileObject.close()
        else:
            """
            to retain the existing osd scenario generated
            while adding new OSD node
            """
            fileObject = open(file_Name % self.hostname, "rb")
            existing_osd_scenario = pickle.load(fileObject)
            lvm_volms.append(
                {
                    "data": existing_osd_scenario[0]["data"],
                    "data_vg": existing_osd_scenario[0]["data_vg"],
                }
            )
            fileObject.close()

        return lvm_volms

    def chk_lvm_exists(self):
        out, rc = self.exec_command(cmd="lsblk")
        if "lvm" in out:
            return 0
        else:
            return 1

    def install_lvm_util(self):
        """
        Installs lvm util
        """
        logger.info("installing lvm util")
        if self.pkg_type == "rpm":
            self.exec_command(cmd="sudo yum install -y lvm2")
        else:
            self.exec_command(cmd="sudo apt-get install -y lvm2")

    def multiple_lvm_scenarios(self, devices, scenario):
        """
        Create lvm volumes based on the provided scenario and return dict, suitable for
        ansible config.

        Args:
            devices (list): device list
            scenario (func): osd scenario to be generated

        Returns (dict): generated osd scenario
        """
        self.install_lvm_util()
        osd_scenarios = {}
        devices_str = " ".join(
            devices
        )  # devices in single string eg: /dev/vdb /dev/vdc /dev/vdd
        file_Name = "osd_scenarios_%s"
        """
        device1,device2,device3 --> devices of the node
        # """
        devices_dict = {"devices": devices_str}
        for dev in devices:
            devices_dict.update({"device%s" % (devices.index(dev)): dev})

        exists = self.chk_lvm_exists()
        if exists == 0:
            """
            for script test_ansible_roll_over.py, which adds new OSD,
            to prevent creation of lvms on the existing osd, using this chk_lvm_exists()

            """
            logger.info("lvms configured already")
            fileObject = open(file_Name % self.hostname, "rb")
            existing_osd_scenarios = pickle.load(fileObject)
            osd_scenarios.update(existing_osd_scenarios)
            fileObject.close()

        else:
            generated_sce_dict = scenario(self, devices_dict)
            osd_scenarios.update(
                {
                    self.hostname: [
                        generated_sce_dict.get("scenario"),
                        {"dmcrypt": generated_sce_dict.get("dmcrypt")},
                        {"batch": generated_sce_dict.get("batch", None)},
                    ]
                }
            )
            logger.info("generated scenario on %s %s" % (self.hostname, scenario))

        fileObject = open(file_Name % self.hostname, "wb")
        pickle.dump(osd_scenarios, fileObject)
        fileObject.close()
        return osd_scenarios

    def get_dir_list(self, dir_path, sudo=False):
        """Lists directories from node

        Args:
            dir_path (str): Directory path to get direcotry list
            sudo (bool): Use root access
        """
        client = self.rssh if sudo else self.ssh
        try:
            return client().open_sftp().listdir(dir_path)
        except FileNotFoundError:
            logger.info(f"Dir path '{dir_path}' not present")
            return None
        except Exception as e:
            raise e

    def get_listdir_attr(self, dir_path, sudo=False):
        """Lists directory attributes from node

        Args:
            dir_path (str): Directory path to get direcotry attributes
            sudo (bool): Use root access
        """
        client = self.rssh if sudo else self.ssh
        try:
            return client().open_sftp().listdir_attr(dir_path)
        except FileNotFoundError:
            logger.info(f"Dir path '{dir_path}' not present")
            return None
        except Exception as e:
            raise e

    def upload_file(self, src, dst, sudo=False):
        """Put file to remote location

        Args:
            src (str): Source file location
            dst (str): File destination location
            sudo (bool): Use root access
        """
        client = self.rssh if sudo else self.ssh
        client().open_sftp().put(src, dst)

    def download_file(self, src, dst, sudo=False):
        """Get file from remote location

        Args:
            src (str): Source file remote location
            dst (str): File destination location
            sudo (bool): Use root access
        """
        client = self.rssh if sudo else self.ssh
        client().open_sftp().get(src, dst)

    def create_dirs(self, dir_path, sudo=False):
        """Create directory on node
        Args:
            dir_path (str): Directory path to create
            sudo (bool): Use root access
        """
        client = self.rssh if sudo else self.ssh
        try:
            client().open_sftp().mkdir(dir_path)
        except Exception:
            # Error happens when the directory already exists
            logger.info("mkdir failed, retrying with -p param")
            cmd = f"mkdir -p {dir_path}"
            self.exec_command(cmd=cmd, sudo=True)

    def remove_file(self, file_path, sudo=False):
        """Remove file from node
        Args:
            file_path (str): file path to delete
            sudo (bool): use root access
        """
        client = self.rssh if sudo else self.ssh
        try:
            client().open_sftp().remove(file_path)
        except Exception:
            logger.info("rm failed, retrying with -rvf param")
            cmd = f"rm -rvf {file_path}"
            self.exec_command(cmd=cmd, sudo=True)


class CephObject(object):
    def __init__(self, role, node):
        """
        Generic Ceph object, works as proxy to exec_command method
        Args:
            role (str): role string
            node (CephNode): node object
        """
        self.role = role
        self.node = node

    @property
    def pkg_type(self):
        return self.node.pkg_type

    @property
    def distro_info(self):
        return self.node.distro_info

    def exec_command(self, cmd, **kw):
        """
        Proxy to node's exec_command
        Args:
            cmd(str): command to execute
            **kw: options

        Returns:
            node's exec_command result
        """
        return self.node.exec_command(cmd=cmd, **kw)

    def create_dirs(self, dir_path, sudo=False):
        """
        Proxy to node's create_dirs
        Args:
            dir_path (str): Directory path to create
            sudo (bool): Use root access

        Returns:
            node's create_dirs result
        """
        return self.node.create_dirs(dir_path=dir_path, sudo=sudo)

    def remote_file(self, **kw):
        """
        Proxy to node's write file
        Args:
            **kw: options

        Returns:
            node's remote_file result
        """
        return self.node.remote_file(**kw)

    def get_dir_list(self, dir_path, sudo=False):
        """
        Proxy to get directory files list

        Args:
            dir_path (str): Directory path to get direcotry list
            sudo (bool): Use root access

        """
        self.node.get_dir_list(dir_path=dir_path, sudo=sudo)

    def upload_file(self, src, dst, sudo=False):
        """
        Proxy to upload file to remote location

        Args:
            src (str): Source file location
            dst (str): File destination location
            sudo (bool): Use root access
        """
        self.node.upload_file(src=src, dst=dst, sudo=sudo)

    def download_file(self, src, dst, sudo=False):
        """
        Proxy to download file from remote location

        Args:
            src (str): Source file location
            dst (str): File destination location
            sudo (bool): Use root access
        """
        self.node.download_file(src=src, dst=dst, sudo=sudo)


class CephDemon(CephObject):
    def __init__(self, role, node):
        """
        Ceph demon representation. Can be containerized.
        Args:
            role(str): Ceph demon type
            node(CephNode): node object
        """
        super(CephDemon, self).__init__(role, node)
        self.containerized = None
        self.__custom_container_name = None
        self.is_active = True

    @property
    def container_name(self):
        return (
            (
                "ceph-{role}-{host}".format(role=self.role, host=self.node.shortname)
                if not self.__custom_container_name
                else self.__custom_container_name
            )
            if self.containerized
            else ""
        )

    @container_name.setter
    def container_name(self, name):
        self.__custom_container_name = name

    @property
    def container_prefix(self):
        distro_ver = self.distro_info["VERSION_ID"]
        if distro_ver.startswith("8"):
            return (
                "sudo podman exec {c_name}".format(c_name=self.container_name)
                if self.containerized
                else ""
            )
        else:
            return (
                "sudo docker exec {c_name}".format(c_name=self.container_name)
                if self.containerized
                else ""
            )

    def exec_command(self, cmd, **kw):
        """
        Proxy to node's exec_command with wrapper to run commands inside the container
        for containerized demons.

        Args:
            cmd(str): command to execute
            **kw: options

        Returns:
            node's exec_command resut
        """
        return (
            self.node.exec_command(
                cmd=" ".join([self.container_prefix, cmd.replace("sudo", "")]), **kw
            )
            if self.containerized
            else self.node.exec_command(cmd=cmd, **kw)
        )

    def ceph_demon_by_container_name(self, container_name):
        distro_ver = self.distro_info["VERSION_ID"]
        if distro_ver.startswith("8"):
            self.exec_command(cmd="sudo podman info")
        else:
            self.exec_command(cmd="sudo docker info")


class CephOsd(CephDemon):
    def __init__(self, node, device=None):
        """
        Represents single osd instance associated with a device.
        Args:
            node (CephNode): ceph node
            device (str): device, can be left unset but must be set during inventory
                          file configuration
        """
        super(CephOsd, self).__init__("osd", node)
        self.device = device

    @property
    def container_name(self):
        return (
            "ceph-{role}-{host}-{device}".format(
                role=self.role, host=self.node.hostname, device=self.device
            )
            if self.containerized
            else ""
        )

    @property
    def is_active(self):
        return True if self.device else False

    @is_active.setter
    def is_active(self, value):
        pass


class CephClient(CephObject):
    def __init__(self, role, node):
        """
        Ceph client representation, works as proxy to exec_command method.
        Args:
            role(str): role string
            node(CephNode): node object
        """
        super(CephClient, self).__init__(role, node)


class CephInstaller(CephObject):
    def __init__(self, role, node):
        """
        Ceph client representation, works as proxy to exec_command method
        Args:
            role(str): role string
            node(CephNode): node object
        """
        super(CephInstaller, self).__init__(role, node)
        self.ansible_dir = "/usr/share/ceph-ansible"

    def append_to_all_yml(self, content):
        """
        Adds content to all.yml
        Args:
            content(str): all.yml config as yml string
        """
        all_yml_file = self.remote_file(
            sudo=True,
            file_name="{}/group_vars/all.yml".format(self.ansible_dir),
            file_mode="a",
        )
        all_yml_file.write(content)
        all_yml_file.flush()
        self.exec_command(
            sudo=True, cmd="chmod 644 {}/group_vars/all.yml".format(self.ansible_dir)
        )

    def get_all_yml(self):
        """
        Returns all.yml content
        Returns:
            dict: all.yml content

        """
        out, err = self.exec_command(
            sudo=True,
            cmd="cat {ansible_dir}/group_vars/all.yml".format(
                ansible_dir=self.ansible_dir
            ),
        )
        return yaml.safe_load(out)

    def get_installed_ceph_versions(self):
        """
        Returns installed ceph versions
        Returns:
            str: ceph vsersions

        """
        if self.pkg_type == "rpm":
            out, rc = self.exec_command(cmd="rpm -qa | grep ceph")
        else:
            out, rc = self.exec_command(sudo=True, cmd="apt-cache search ceph")
        return out

    def write_inventory_file(self, inventory_config, file_name="hosts"):
        """
        Write inventory to hosts file for ansible use. Old file will be overwritten
        Args:
            inventory_config(str):inventory config compatible with ceph-ansible
            file_name(str): custom inventory file name. (default : "hosts")
        """
        host_file = self.remote_file(
            sudo=True,
            file_mode="w",
            file_name="{ansible_dir}/{inventory_file}".format(
                ansible_dir=self.ansible_dir, inventory_file=file_name
            ),
        )
        logger.info(inventory_config)
        host_file.write(inventory_config)
        host_file.flush()

        out, rc = self.exec_command(
            sudo=True,
            cmd="cat {ansible_dir}/{inventory_file}".format(
                ansible_dir=self.ansible_dir, inventory_file=file_name
            ),
        )
        out = out.rstrip("\n")
        out = re.sub(r"\]+", "]", out)
        out = re.sub(r"\[+", "[", out)
        host_file = self.remote_file(
            sudo=True,
            file_mode="w",
            file_name="{ansible_dir}/{inventory_file}".format(
                ansible_dir=self.ansible_dir, inventory_file=file_name
            ),
        )
        host_file.write(out)
        host_file.flush()

    def append_inventory_file(self, inventory_config, file_name="hosts"):
        """
        Append inventory to hosts file for ansible use. Existing file will be appended
        Args:
            inventory_config(str):inventory config compatible with ceph-ansible
            file_name(str): custom inventory file name. (default : "hosts")
        """
        host_file = self.remote_file(
            sudo=True,
            file_mode="a",
            file_name="{ansible_dir}/{inventory_file}".format(
                ansible_dir=self.ansible_dir, inventory_file=file_name
            ),
        )
        logger.info(inventory_config)
        host_file.write(inventory_config)
        host_file.flush()

        out, rc = self.exec_command(
            sudo=True,
            cmd="cat {ansible_dir}/{inventory_file}".format(
                ansible_dir=self.ansible_dir, inventory_file=file_name
            ),
        )
        out = out.rstrip("\n")
        out = re.sub(r"\]+", "]", out)
        out = re.sub(r"\[+", "[", out)
        host_file = self.remote_file(
            sudo=True,
            file_mode="a",
            file_name="{ansible_dir}/{inventory_file}".format(
                ansible_dir=self.ansible_dir, inventory_file=file_name
            ),
        )
        host_file.write(out)
        host_file.flush()

    def read_inventory_file(self):
        """
        Read inventory file from ansible node
        Returns:
            out : inventory file data
        """
        out, err = self.exec_command(
            sudo=True,
            cmd="cat {ansible_dir}/hosts".format(ansible_dir=self.ansible_dir),
        )
        return out.splitlines()

    def setup_ansible_site_yml(self, build, containerized):
        """
        Create site.yml from sample for RPM or Image based deployment.

        Args:
            build(string): RHCS build
            containerized(bool): use site-container.yml.sample if True else site.yml.sample
        """
        # https://github.com/ansible/ansible/issues/11536
        self.exec_command(
            cmd="""echo 'export ANSIBLE_SSH_CONTROL_PATH="%(directory)s/%%C"'>> ~/.bashrc;
                                 source ~/.bashrc"""
        )

        file_name = "site.yml"

        if containerized:
            file_name = "site-container.yml"

        self.exec_command(
            sudo=True,
            cmd="cp -R {ansible_dir}/{file_name}.sample {ansible_dir}/{file_name}".format(
                ansible_dir=self.ansible_dir, file_name=file_name
            ),
        )

        cmd1 = r" -type f -exec chmod 644 {} \;"
        cmd = f"find {self.ansible_dir}" + cmd1
        self.exec_command(sudo=True, cmd=cmd)

    def install_ceph_ansible(self, rhbuild, **kw):
        """
        Installs ceph-ansible
        """
        logger.info("Installing ceph-ansible")

        ansible_rpm = {
            "2": {"7": "rhel-7-server-ansible-2.4-rpms"},
            "3": {"7": "rhel-7-server-ansible-2.6-rpms"},
            "4": {
                "7": "rhel-7-server-ansible-2.9-rpms",
                "8": "ansible-2.9-for-rhel-8-x86_64-rpms",
            },
            "5": {"8": "ansible-2.9-for-rhel-8-x86_64-rpms"},
        }

        if self.pkg_type == "deb":
            self.exec_command(sudo=True, cmd="apt-get install -y ceph-ansible")
        else:
            distro_ver = self.distro_info["VERSION_ID"].split(".")[0]
            rhcs_ver = rhbuild.split(".")[0]
            # Use ansible 2.8 for rhcs 4.1.z
            if str(rhbuild).startswith("4.1"):
                ansible_rpm[rhcs_ver]["7"] = "rhel-7-server-ansible-2.8-rpms"
                ansible_rpm[rhcs_ver]["8"] = "ansible-2.8-for-rhel-8-x86_64-rpms"

            try:
                rpm = ansible_rpm[rhcs_ver][distro_ver]
                self.exec_command(
                    cmd="sudo subscription-manager repos --enable={}".format(rpm),
                    long_running=True,
                )
            except KeyError as err:
                raise KeyError(err)
            except CommandFailed as err:
                raise CommandFailed(err)

        if kw.get("upgrade"):
            self.exec_command(sudo=True, cmd="yum update meta", check_ec=False)
            self.exec_command(sudo=True, cmd="yum update -y ansible ceph-ansible")
        else:
            self.exec_command(sudo=True, cmd="yum install -y ceph-ansible")

        if self.pkg_type == "deb":
            out, rc = self.exec_command(cmd="dpkg -s ceph-ansible")
        else:
            out, rc = self.exec_command(cmd="rpm -qa | grep ceph-ansible")
        output = out.rstrip()
        logger.info("Installed ceph-ansible: {version}".format(version=output))

    def add_iscsi_settings(self, test_data):
        """
        Add iscsi config to iscsigws.yml
        Args:
            test_data: test data dict
        """
        iscsi_file = self.remote_file(
            sudo=True,
            file_name="{}/group_vars/iscsigws.yml".format(self.ansible_dir),
            file_mode="a",
        )
        iscsi_file.write(test_data["luns_setting"])
        iscsi_file.write(test_data["initiator_setting"])
        iscsi_file.write(test_data["gw_ip_list"])
        iscsi_file.flush()

    def enable_ceph_mgr_restful(self):
        """
        Enable restful service from MGR module with self-signed certificate.
        Returns:
            user_cred: user credentials for restful calls
        """
        try:
            # enable restful service from MGR module
            out, err = self.exec_command(
                sudo=True, cmd="ceph mgr module enable restful"
            )
            if err:
                raise CommandFailed(err)
            logger.info(out)

            # Start restful service with self-signed certificate
            out, err = self.exec_command(
                sudo=True, cmd="ceph restful create-self-signed-cert"
            )
            if err:
                raise CommandFailed(err)
            logger.info(out)

            # Create new restful user
            user = "test_{}".format(int(time()))
            cred, err = self.exec_command(
                sudo=True, cmd="ceph restful create-key {user}".format(user=user)
            )

            return {"user": user, "password": cred.strip()}
        except CommandFailed as err:
            logger.error(err.args)
        return False


class CephObjectFactory(object):
    DEMON_ROLES = ["mon", "osd", "mgr", "rgw", "mds", "nfs", "grafana"]
    CLIENT_ROLES = ["client"]

    def __init__(self, node):
        """
        Factory for Ceph objects.
        Args:
            node: node object
        """
        self.node = node

    def create_ceph_object(self, role):
        """
        Create an appropriate Ceph object by role
        Args:
            role: role string

        Returns:
        Ceph object based on role
        """
        if role == "installer":
            return CephInstaller(role, self.node)
        if role == self.CLIENT_ROLES:
            return CephClient(role, self.node)
        if role == "osd":
            free_volume_list = self.node.get_free_volumes()
            if len(free_volume_list) > 0:
                free_volume_list[0].status = NodeVolume.ALLOCATED
            else:
                raise RuntimeError(
                    f"{self.node.vmname} does not have 'no-of-volumes' key defined in the inventory file"
                )
            return CephOsd(self.node)
        if role in self.DEMON_ROLES:
            return CephDemon(role, self.node)
        if role != "pool":
            return CephObject(role, self.node)
