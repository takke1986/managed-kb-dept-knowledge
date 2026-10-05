#!/usr/bin/env node
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { PrototypeStack } from '../lib/prototype-stack';
import { WafStack } from '../lib/waf-stack';

const config = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'config', 'app.json'), 'utf-8'));
const account = process.env.CDK_DEFAULT_ACCOUNT;
const app = new cdk.App();
const waf = new WafStack(app, 'ManagedKbPrototypeWaf', {
  env: { account, region: 'us-east-1' }, // CloudFront 用の WAF は us-east-1
  crossRegionReferences: true,
  prefix: config.prefix,
  description: 'WAF for the Managed Knowledge Base prototype CloudFront distribution',
});
new PrototypeStack(app, 'ManagedKbPrototype', {
  env: { account, region: config.region },
  crossRegionReferences: true,
  webAclArn: waf.webAclArn,
  description: 'Managed Knowledge Base prototype with department level access control',
});
