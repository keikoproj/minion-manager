# Spot Instances use and management for Kubernetes

The minion-manager enables the intelligent use of Spot Instances in Kubernetes.

## What does it do?

* The minion-manager operates on autoscaling groups (ASGs).

* It queries AWS for all autoscaling groups that have the Kubernetes cluster tag and a special tag called "k8s-minion-manager". ASGs which have these tags are operated upon by the minion-manager.

* It queries AWS to get the pricing information for spot-instances every 10 minutes.

* It checks whether the given ASGs are using spot-instances or on-demand instances. If the spot-instance price < on-demand instance price, it switches the ASG to use spot-instances and terminates the on-demand instance.

* If, at any point in time, the spot-instance price spikes and goes above on-demand instance price, it switches the ASG to use on-demand instances.

### Prerequisites

It's best to run the minion-manger on an on-demand instance.

The IAM role of the node that runs the minion-manager should have the following policies.

```
{
    "Sid": "kopsK8sMinionManager",
    "Effect": "Allow",
    "Action": [
        "ec2:DescribeInstances",
        "ec2:TerminateInstances",
        "ec2:DescribeSpotPriceHistory",
        "ec2:DescribeSpotInstanceRequests",
        "autoscaling:CreateLaunchConfiguration",
        "autoscaling:DeleteLaunchConfiguration",
        "autoscaling:DescribeLaunchConfigurations",
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:TerminateInstanceInAutoScalingGroup",
        "autoscaling:UpdateAutoScalingGroup",
        "autoscaling:DescribeScalingActivities",
        "iam:PassRole"
    ],
    "Resource": [
	"*"
    ]
}
```

### Installing

Modify the `deploy/mm.yaml` by

1) Add the names of your cluster instead of <my-cluster-name>
2) Change the namespace where the minion-manager will be run.

Then, `kubectl apply -f deploy/mm.yaml`.

**Design:**

* Only ASGs which have the "k8s-minion-manager" tag are considered by the minion-manager. Other ASGs are left alone.
* Minion-manager queries AWS for ASGs with these tags every "--refresh-interval". Default is 5 minutes.
* The "k8s-minion-manager" tag can have two possible values:
    * "use-spot": This will make the minion-manager intelligently use spot instances in the ASG
    * "no-spot": This will make the minion-manager always use on-demand instances in the ASG. This is useful when someone wants to temporarily switch to on-demand instances and at a later point switch to "use-spot"
    * Note that after changing the tag value, it may take upto 5 minutes for the minion-manager pod to see the changes and make them take effect.
* The "k8s-minion-manager/not-terminate" tag can control ASG instance terminate by the minion-manager. If you want to control when to terminate ASG instances. You can set this tag to `true`. If not set or other value will disable this feature.

**What happens when:**

_1. User runs k8s-minion-manager without any ASG having the "k8s-minion-manager" tag?_

k8s-minion-manager ignores all ASGs. It simply continues to keep polling AWS for the tags every "refresh-interval" seconds.

_2. User runs k8s-minion-manager, adds the "k8s-minion-manager" tag and the "use-spot" value to start with. But later wants to not use spot instances._

User should then change the key from "use-spot" to "no-spot". This will indicate to the k8s-minion-manager that the ASG should have all on-demand instances and it will make sure of that.

_3. User runs k8s-minion-manager, adds the "k8s-minion-manager" key and the "use-spot" value to start with. But later simply removes the tag and the value._

Once the tag is removed, k8s-minion-manager simply considers the ASG to be off-limits and does not act upon it. The ASG will remain in whatever condition it is in.

_4. User runs k8s-minion-manager, adds the "k8s-minion-manager" key and the "no-spot" value to start with. But later simply removes the tag and the value._

Same as above.  The ASG will remain in whatever condition it is in.

_5. User is running k8s-minion-manager and using spot instances. But now wants to stop using instances forever._

This will be a multi-step process:
* Change the value of the "k8-minion-manager" tag to "no-spot".
* Wait for the minion-manager to react to this and switch the instances to on-demand. Look at the AWS console for verifying that all instances are on-demand.
* After the above, remove the "k8s-minion-manager" tag.
* Delete the "k8s-minion-manager" deployment.

**How do I:**

 _1. Run unit tests: Ensure that your AWS cli is set up correctly. Then simply run `make docker-test`_
