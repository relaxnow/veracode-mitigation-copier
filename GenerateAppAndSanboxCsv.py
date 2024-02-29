from veracode_api_py.api import Applications, Sandboxes

print("Applications Application Name,Scans Sandbox Name")
guid = ""
app_candidates = Applications().get_all()
for application_candidate in app_candidates:
        print(application_candidate["profile"]["name"]+",Policy Sandbox")
        guid = application_candidate["guid"]
        sandboxes = Sandboxes().get_all(guid)
        for sandbox in sandboxes:
            print(application_candidate["profile"]["name"]+","+sandbox["name"])