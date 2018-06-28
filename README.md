# Spot Instances use and management for Kubernetes

The minion-manager enables the intelligent use of Spot Instances in Kubernetes.

## What does it do?

* The minion-manager operates on autoscaling groups (ASGs) that are passed to it as command line arguments.

* It queries AWS to get the pricing information for spot-instances every 10 minutes.

* It checks whether the given ASGs are using spot-instances or on-demand instances. If the spot-instance price < on-demand instance price, it switches the ASG to use spot-instances and terminates the on-demand instance.

* If, at any point in time, the spot-instance price spikes and goes above on-demand instance price, it switches the ASG to use on-demand instances.

### Prerequisites

It's best to run the minion-manger on an on-demand instance.

Most of the testing done on current code has had the minion-manager run on the Kubernetes master.

The IAM role of the node that runs the minion-manager should have the following policies.

```
{
    "Sid": "kopsK8sMinionManager",
    "Effect": "Allow",
    "Action": [
	"ec2:TerminateInstances",
	"ec2:DescribeSpotPriceHistory",
	"autoscaling:CreateLaunchConfiguration",
	"autoscaling:DeleteLaunchConfiguration",
	"autoscaling:DescribeLaunchConfiguration",
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:TerminateInstanceInAutoScalingGroup",
        "autoscaling:UpdateAutoScalingGroup",
	"iam:PassRole"
    ],
    "Resource": [
	"*"
    ]
}
```

### Installing

Modify the `deploy/mm.yaml` by

1) Add the names of the Autoscaling Groups instead of "<asg_name_N>"
2) Change the namespace where the minion-manager will be run.

Then, `kubectl apply -f deploy/mm.yaml`.

