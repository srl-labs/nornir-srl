from nornir import InitNornir
from nornir_utils.plugins.functions import print_result
#from nornir_srl.gnmi import get_path
#from nornir_srl.load import Intent
from nornir_srl.tasks import gnmi_get, gnmi_set

#def main():
#intents = Intent.from_files("intent")
nr0 = InitNornir(config_file="nornir_config.yaml")
# result = nr0.run(task=gnmi_get, type="config", strip_module=True, paths=["/interface[name=ethernet-1/48]", "/interface[name=ethernet-1/49]"])
p = [
        ("interface[name=ethernet-1/48]", 
        {
            "description": "itf description e-1/48",
            "admin-state": "enable",
            "mtu": 1501,
        })
]
#result = nr0.run(task=gnmi_get, type="config", paths=["/system/gnmi-server"], strip_module=True)
result = nr0.run(task=gnmi_set, action="update", dry_run=False, paths=p, encoding="json_ietf")
print_result(result)

# if __name__ == "__main__":
#     main()


