import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

/**
 * CloudFront に付ける WAF。CloudFront 用の WAF は us-east-1 に作る決まりなので、別のスタックにする。
 *
 * - AWS の共通ルール（よくある攻撃）と、既知の悪い入力のルール
 * - 1つの IP からの呼び出しが5分で1000回を超えたら止める（チャットは生成AIを呼ぶので、使いすぎの歯止め）
 */
export class WafStack extends cdk.Stack {
  readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: cdk.StackProps & { prefix: string }) {
    super(scope, id, props);
    const managed = (name: string, priority: number): wafv2.CfnWebACL.RuleProperty => ({
      name, priority,
      statement: { managedRuleGroupStatement: { vendorName: 'AWS', name } },
      overrideAction: { none: {} },
      visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: name, sampledRequestsEnabled: true },
    });
    const acl = new wafv2.CfnWebACL(this, 'WebAcl', {
      name: `${props.prefix}-web`,
      description: 'Managed KB prototype - common rules and a per IP rate limit',
      scope: 'CLOUDFRONT',
      defaultAction: { allow: {} },
      visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: `${props.prefix}-web`, sampledRequestsEnabled: true },
      rules: [
        managed('AWSManagedRulesCommonRuleSet', 1),
        managed('AWSManagedRulesKnownBadInputsRuleSet', 2),
        {
          name: 'RateLimitPerIp',
          priority: 3,
          statement: { rateBasedStatement: { limit: 1000, aggregateKeyType: 'IP' } },
          action: { block: {} },
          visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'RateLimitPerIp', sampledRequestsEnabled: true },
        },
      ],
    });
    this.webAclArn = acl.attrArn;
  }
}
