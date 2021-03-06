import kopf
import kubernetes
import os

WATCH_NAMESPACE = os.getenv('WATCH_NAMESPACE', "")
all_namespaces  = WATCH_NAMESPACE.split(',')
def watch_namespace(namespace, **_):
    if WATCH_NAMESPACE == "" or namespace in all_namespaces:
        return True
    return False

@kopf.on.create('', 'v1', 'secrets', annotations={'synator/sync': 'yes'}, when=watch_namespace)
@kopf.on.update('', 'v1', 'secrets', annotations={'synator/sync': 'yes'}, when=watch_namespace)
def update_secret(body, meta, spec, status, old, new, diff, **kwargs):
    api = kubernetes.client.CoreV1Api()
    namespace_response = api.list_namespace()
    namespaces = [nsa.metadata.name for nsa in namespace_response.items]
    namespaces.remove(meta.namespace)

    secret = api.read_namespaced_secret(meta.name, meta.namespace)
    secret.metadata.annotations.pop('synator/sync')
    secret.metadata.annotations.pop('field.cattle.io/projectId')
    secret.metadata.resource_version = None
    secret.metadata.uid = None
    for ns in parse_target_namespaces(meta, namespaces):
        secret.metadata.namespace = ns
        # try to pull the Secret object then patch it, try creating it if we can't
        try:
            api.read_namespaced_secret(meta.name, ns)
            api.patch_namespaced_secret(meta.name, ns, secret)
        except kubernetes.client.rest.ApiException as e:
            print(e.args)
            api.create_namespaced_secret(ns, secret)


@kopf.on.create('', 'v1', 'configmaps', annotations={'synator/sync': 'yes'}, when=watch_namespace)
@kopf.on.update('', 'v1', 'configmaps', annotations={'synator/sync': 'yes'}, when=watch_namespace)
def updateConfigMap(body, meta, spec, status, old, new, diff, **kwargs):
    api = kubernetes.client.CoreV1Api()
    namespace_response = api.list_namespace()
    namespaces = [nsa.metadata.name for nsa in namespace_response.items]
    namespaces.remove(meta.namespace)

    cfg = api.read_namespaced_config_map(meta.name, meta.namespace)
    cfg.metadata.annotations.pop('synator/sync')
    cfg.metadata.annotations.pop('field.cattle.io/projectId')
    cfg.metadata.resource_version = None
    cfg.metadata.uid = None
    for ns in parse_target_namespaces(meta, namespaces):
        cfg.metadata.namespace = ns
        # try to pull the ConfigMap object then patch it, try to create it if we can't
        try:
            api.read_namespaced_config_map(meta.name, ns)
            api.patch_namespaced_config_map(meta.name, ns, cfg)
        except kubernetes.client.rest.ApiException as e:
            print(e.args)
            api.create_namespaced_config_map(ns, cfg)


def parse_target_namespaces(meta, namespaces):
    namespace_list = []
    # look for a namespace inclusion label first, if we don't find that, assume all namespaces are the target
    if 'synator/include-namespaces' in meta.annotations:
        value = meta.annotations['synator/include-namespaces']
        namespaces_to_include = value.replace(' ', '').split(',')
        for ns in namespaces_to_include:
            if ns in namespaces:
                namespace_list.append(ns)
            else:
                print(
                    f"WARNING: include-namespaces requested I add this resource to a non-existing namespace: {ns}")
    else:
        # we didn't find a namespace inclusion label, so let's see if we were told to exclude any
        namespace_list = namespaces
        if 'synator/exclude-namespaces' in meta.annotations:
            value = meta.annotations['synator/exclude-namespaces']
            namespaces_to_exclude = value.replace(' ', '').split(',')
            if len(namespaces_to_exclude) < 1:
                print(
                    "WARNING: exclude-namespaces was specified, but no values were parsed")

            for ns in namespaces_to_exclude:
                if ns in namespace_list:
                    namespace_list.remove(ns)
                else:
                    print(
                        f"WARNING: I was told to exclude namespace {ns}, but it doesn't exist on the cluster")

    return namespace_list


@kopf.on.create('', 'v1', 'namespaces')
def newNamespace(spec, name, meta, logger, **kwargs):
    api = kubernetes.client.CoreV1Api()

    try:
        api_response = api.list_secret_for_all_namespaces()
        # TODO: Add configmap
        for secret in api_response.items:
            # Check secret have annotation
            if secret.metadata.annotations and secret.metadata.annotations.get("synator/sync") == "yes":
                secret.metadata.annotations.pop('synator/sync')
                secret.metadata.annotations.pop('field.cattle.io/projectId')
                secret.metadata.resource_version = None
                secret.metadata.uid = None
                for ns in parse_target_namespaces(secret.metadata, [name]):
                    secret.metadata.namespace = ns
                    try:
                        api.read_namespaced_secret(
                            secret.metadata.name, ns)
                        api.patch_namespaced_secret(
                            secret.metadata.name, ns, secret)
                    except kubernetes.client.rest.ApiException as e:
                        print(e.args)
                        api.create_namespaced_secret(ns, secret)
    except kubernetes.client.rest.ApiException as e:
        print("Exception when calling CoreV1Api->list_secret_for_all_namespaces: %s\n" % e)


# Reload deployment when update configmap or secret

@kopf.on.update('', 'v1', 'configmaps', when=watch_namespace)
def reload_deployment_config(body, meta, spec, status, old, new, diff, logger, **kwargs):
    reload_deployments_sync(meta, 'configmap', logger)

@kopf.on.update('', 'v1', 'secrets', when=watch_namespace)
def reload_deployment_secret(body, meta, spec, status, old, new, diff, logger, **kwargs):
    reload_deployments_sync(meta, 'secret', logger)

def reload_deployments_sync(meta, secretOrConfigmap, logger):
    try:
        # Get namespace
        ns = meta.namespace
        api = kubernetes.client.AppsV1Api()
        deployments = api.list_namespaced_deployment(ns)
        configSearch = secretOrConfigmap + ':' + meta.name

        logger.info(f"NS: %s Name: %s Deployments %s", ns, configSearch, str(len(deployments.items)))

        for deployment in deployments.items:
            annotations = deployment.spec.template.metadata.annotations
            syncReloads = []
            if annotations and annotations.get('synator/reload'):
                syncReloads = annotations.get('synator/reload').split(',')
            
            if any(configSearch in s for s in syncReloads):
                # Reload deployment
                update_deployment(api, deployment, logger)
    except kubernetes.client.rest.ApiException as e:
        print("Exception when calling AppsV1Api: %s\n" % e)

def update_deployment(api, deployment, logger):
    # Update revision
    revision = deployment.spec.template.metadata.annotations.get('synator/revision')
    if revision is None:
        revision = 1
    else:
        revision = int(revision) + 1

    deployment.spec.template.metadata.annotations['synator/revision'] = str(revision)

    # Update the deployment
    api_response = api.patch_namespaced_deployment(
        name=deployment.metadata.name,
        namespace=deployment.metadata.namespace,
        body=deployment)

    # print("Deployment updated. status='%s'" % str(api_response.status))

    logger.info(f"Deployment %s updated revision %s", deployment.metadata.name, str(revision))
