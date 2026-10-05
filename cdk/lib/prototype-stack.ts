import * as path from 'node:path';
import * as fs from 'node:fs';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cloudtrail from 'aws-cdk-lib/aws-cloudtrail';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as guardduty from 'aws-cdk-lib/aws-guardduty';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as eventsources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cr from 'aws-cdk-lib/custom-resources';

const ROOT = path.join(__dirname, '..', '..');
const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'config', 'app.json'), 'utf-8'));
const TARGET_NAME = 'kb';

/**
 * Managed KB を部署ごとに分けて使う試作。
 *
 *   画面 → API（Lambda）→ エージェント → AgentCore Gateway → Managed KB
 *
 * 部署の分離は、スライド「Agentic RAG の Fine-grained access control」の強化構成に従う。
 *   ツール層:   Interceptor が JWT から userContext を入れる / Cedar が JWT と食い違えば拒否
 *   リソース層: KB の ACL で部署の人の文書だけ返す / KB のリソースポリシーで Gateway 以外からの検索を拒否
 */
export class PrototypeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: cdk.StackProps & { webAclArn: string }) {
    super(scope, id, props);
    const prefix: string = config.prefix;

    // ---------------- 画面の置き場所 ----------------
    const webBucket = new s3.Bucket(this, 'WebBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    const distribution = new cloudfront.Distribution(this, 'Web', {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(webBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      },
      defaultRootObject: 'index.html',
      webAclId: props.webAclArn,
    });
    const webOrigin = `https://${distribution.distributionDomainName}`;

    // ---------------- 文書の置き場所 ----------------
    // 利用者が Storage Browser で使うファイル置き場。最上位のフォルダが部署
    // 元ファイルと書き起こしは、スタックを消しても残す。版を持ち、誤って消した・上書きしたものを戻せる
    const keepVersions = [{ noncurrentVersionExpiration: cdk.Duration.days(90) }]; // 古い版は90日で消す
    const files = new s3.Bucket(this, 'Files', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      lifecycleRules: keepVersions,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      eventBridgeEnabled: true, // GuardDuty のマルウェア検査が S3 のイベントを受けるのに要る
    });
    // マルウェアの疑いがあるファイルは、誰も読めない（ダウンロード・変換・検索結果から開く、のどれも）
    files.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'DenyReadOfInfectedObjects',
      effect: iam.Effect.DENY,
      principals: [new iam.AnyPrincipal()],
      actions: ['s3:GetObject', 's3:GetObjectVersion'],
      resources: [files.arnForObjects('*')],
      conditions: { StringEquals: { 's3:ExistingObjectTag/GuardDutyMalwareScanStatus': 'THREATS_FOUND' } },
    }));

    // GuardDuty のマルウェア検査（S3 向け）。置かれたファイルを検査し、結果をタグと EventBridge に出す
    const malwareRole = new iam.Role(this, 'MalwareScanRole', {
      assumedBy: new iam.ServicePrincipal('malware-protection-plan.guardduty.amazonaws.com'),
      description: 'GuardDuty Malware Protection for the files bucket',
    });
    const managedRule = `arn:aws:events:${this.region}:${this.account}:rule/DO-NOT-DELETE-AmazonGuardDutyMalwareProtectionS3*`;
    malwareRole.addToPolicy(new iam.PolicyStatement({
      actions: ['events:PutRule', 'events:DeleteRule', 'events:PutTargets', 'events:RemoveTargets'],
      resources: [managedRule],
      conditions: { StringLike: { 'events:ManagedBy': 'malware-protection-plan.guardduty.amazonaws.com' } },
    }));
    malwareRole.addToPolicy(new iam.PolicyStatement({
      actions: ['events:DescribeRule', 'events:ListTargetsByRule'], resources: [managedRule],
    }));
    malwareRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:PutObjectTagging', 's3:GetObjectTagging', 's3:PutObjectVersionTagging', 's3:GetObjectVersionTagging',
        's3:GetObject', 's3:GetObjectVersion'],
      resources: [files.arnForObjects('*')],
    }));
    malwareRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:PutBucketNotification', 's3:GetBucketNotification', 's3:ListBucket'], resources: [files.bucketArn],
    }));
    malwareRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:PutObject'], resources: [files.arnForObjects('malware-protection-resource-validation-object')],
    }));
    const malwarePlan = new guardduty.CfnMalwareProtectionPlan(this, 'MalwareScan', {
      role: malwareRole.roleArn,
      protectedResource: { s3Bucket: { bucketName: files.bucketName } },
      actions: { tagging: { status: 'ENABLED' } },
    });
    malwarePlan.node.addDependency(malwareRole);
    // マルウェアの疑いがあるファイルのタグは、GuardDuty 以外は書き換えも削除もできない（書き換えられると、
    // 読めなくしたファイルを読めるようにできてしまう）。ほかのファイルのタグは書ける（コピーに要る）
    files.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'OnlyGuardDutyRetagsInfectedObjects',
      effect: iam.Effect.DENY,
      principals: [new iam.AnyPrincipal()],
      actions: ['s3:PutObjectTagging', 's3:DeleteObjectTagging', 's3:PutObjectVersionTagging', 's3:DeleteObjectVersionTagging'],
      resources: [files.arnForObjects('*')],
      conditions: {
        StringEquals: { 's3:ExistingObjectTag/GuardDutyMalwareScanStatus': 'THREATS_FOUND' },
        ArnNotEquals: { 'aws:PrincipalArn': malwareRole.roleArn },
      },
    }));

    // 書き起こし・同期の印など、利用者が触らない置き場所。KB はここを読む
    const docs = new s3.Bucket(this, 'Docs', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      lifecycleRules: keepVersions,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // 文書の台帳（元ファイル・書き起こし・タグ・状態の紐付けの正本）と、中身ごとの書き起こしの控え。
    // スタックを消しても残し、ポイントインタイムリカバリ（35日）で戻せるようにする
    const ledgerTable = (id: string, partitionKey: dynamodb.Attribute, sortKey?: dynamodb.Attribute) =>
      new dynamodb.Table(this, id, {
        partitionKey, sortKey,
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        deletionProtection: true,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });
    const documents = ledgerTable('Documents',
      { name: 'department', type: dynamodb.AttributeType.STRING }, { name: 'key', type: dynamodb.AttributeType.STRING });
    documents.addGlobalSecondaryIndex({ indexName: 'bySha', partitionKey: { name: 'sha', type: dynamodb.AttributeType.STRING } });
    documents.addGlobalSecondaryIndex({
      indexName: 'byStatus',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'updatedAt', type: dynamodb.AttributeType.STRING },
    });
    const contents = ledgerTable('Contents', { name: 'sha', type: dynamodb.AttributeType.STRING });
    // 置いた人へのお知らせ（書き起こしの失敗・対象外・ブロック）。90日で消える
    // 部署ごとのタグの一覧（説明・色・並び順・アーカイブ）。管理者が画面で変える
    const tagsTable = ledgerTable('Tags',
      { name: 'department', type: dynamodb.AttributeType.STRING }, { name: 'name', type: dynamodb.AttributeType.STRING });
    const notificationsTable = new dynamodb.Table(this, 'Notifications', {
      partitionKey: { name: 'user', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expiresAt',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const ledgerEnv = {
      DOCUMENTS_TABLE: documents.tableName, CONTENTS_TABLE: contents.tableName,
      NOTIFICATIONS_TABLE: notificationsTable.tableName, NOTIFY_FROM: config.notifyFrom ?? '',
    };
    const grantLedger = (fn: lambda.IFunction) => {
      documents.grantReadWriteData(fn);
      contents.grantReadWriteData(fn);
      notificationsTable.grantReadWriteData(fn);
      if (config.notifyFrom) {
        fn.addToRolePolicy(new iam.PolicyStatement({ actions: ['ses:SendEmail'], resources: ['*'],
          conditions: { StringEquals: { 'ses:FromAddress': config.notifyFrom } } }));
      }
    };

    // ---------------- 利用者（部署 = Cognito のグループ） ----------------
    const preToken = this.pythonFunction('PreToken', 'pretoken', {});
    const userPool = new cognito.UserPool(this, 'Users', {
      userPoolName: `${prefix}-users`,
      featurePlan: cognito.FeaturePlan.ESSENTIALS, // アクセストークンにクレームを足すのに必要
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    userPool.addTrigger(cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG, preToken, cognito.LambdaVersion.V2_0);
    for (const d of config.departments) {
      new cognito.CfnUserPoolGroup(this, `Group-${d.id}`, {
        userPoolId: userPool.userPoolId, groupName: d.id, description: d.id,
      });
    }
    new cognito.CfnUserPoolGroup(this, 'Group-admins', { // 部署ではない。タグの一覧を変えられる
      userPoolId: userPool.userPoolId, groupName: 'admins', description: 'Can edit department tag lists',
    });
    const client = userPool.addClient('WebClient', {
      authFlows: { userPassword: true, userSrp: true }, // userSrp は画面の Amplify のログイン
      generateSecret: false,
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(1),
      preventUserExistenceErrors: true,
    });
    const authEnv = { USER_POOL_ID: userPool.userPoolId, USER_POOL_CLIENT_ID: client.userPoolClientId };

    // ---------------- Managed Knowledge Base ----------------
    const kbRole = new iam.Role(this, 'KbRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }),
      description: 'Managed KB service role (reads kb-source only)',
    });
    docs.grantRead(kbRole, 'kb-source/*');
    kbRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket'], resources: [docs.bucketArn],
      conditions: { StringLike: { 's3:prefix': ['kb-source/*', 'kb-source/'] } },
    }));

    const kb = new bedrock.CfnKnowledgeBase(this, 'Kb', {
      name: `${prefix}-kb`,
      description: 'Department separated managed knowledge base prototype',
      roleArn: kbRole.roleArn,
      knowledgeBaseConfiguration: {
        type: 'MANAGED',
        managedKnowledgeBaseConfiguration: { embeddingModelType: 'MANAGED' },
      },
    });
    kb.node.addDependency(kbRole);

    const dataSource = new bedrock.CfnDataSource(this, 'KbSource', {
      knowledgeBaseId: kb.attrKnowledgeBaseId,
      name: `${prefix}-kb-source`,
      description: 'Markdown transcribed by the convert function, with ACL per document',
      dataDeletionPolicy: 'DELETE',
      dataSourceConfiguration: {
        type: 'MANAGED_KNOWLEDGE_BASE_CONNECTOR',
        managedKnowledgeBaseConnectorConfiguration: {
          connectorParameters: {
            type: 'S3',
            version: '1',
            aclEnabled: true, // ACL の無い文書は取り込まれない
            connectionConfiguration: { bucketName: docs.bucketName, bucketOwnerAccountId: this.account },
            filterConfiguration: { inclusionPrefixes: ['kb-source/'] },
          },
        },
      },
      vectorIngestionConfiguration: { parsingConfiguration: { parsingStrategy: 'SMART_PARSING' } },
    });

    // ---------------- Storage Browser の認証情報 ----------------
    // Storage Browser に渡す認証情報のロール。引き受けられるのは API の関数だけで、
    // 引き受けるときにセッションポリシーで部署のフォルダに絞る
    const apiRole = new iam.Role(this, 'ApiRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
    });
    const storageRole = new iam.Role(this, 'StorageUserRole', {
      assumedBy: new iam.ArnPrincipal(apiRole.roleArn),
      description: 'Storage Browser access, narrowed per department by a session policy',
      maxSessionDuration: cdk.Duration.hours(1),
    });
    files.grantReadWrite(storageRole);
    files.grantDelete(storageRole);
    storageRole.addToPolicy(new iam.PolicyStatement({
      // コピー（名前の変更）はタグを読み書きする。マルウェアの疑いがあるファイルのタグはバケットポリシーで拒否
      actions: ['s3:AbortMultipartUpload', 's3:ListMultipartUploadParts', 's3:GetObjectTagging', 's3:PutObjectTagging'],
      resources: [files.arnForObjects('*')],
    }));
    storageRole.grantAssumeRole(apiRole);


    // ---------------- 取り込み（変換・同期・ACL） ----------------
    const sync = this.pythonFunction('Sync', 'sync', {
      DOCS_BUCKET: docs.bucketName,
      KNOWLEDGE_BASE_ID: kb.attrKnowledgeBaseId,
      DATA_SOURCE_ID: dataSource.attrDataSourceId,
    });
    docs.grantReadWrite(sync, 'control/*');
    docs.grantDelete(sync, 'control/*');
    sync.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:StartIngestionJob', 'bedrock:ListIngestionJobs'], resources: [kb.attrKnowledgeBaseArn],
    }));
    new events.Rule(this, 'SyncSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(sync)],
    });

    const ingestEnv = {
      DOCS_BUCKET: docs.bucketName, FILES_BUCKET: files.bucketName, SYNC_FUNCTION_NAME: sync.functionName,
      ...ledgerEnv, ...authEnv,
    };
    const convert = this.pythonFunction('Convert', 'convert', ingestEnv, {
      timeout: cdk.Duration.minutes(15), memorySize: 3008, // PDF のページ画像を並列に扱うため
    });
    files.grantRead(convert);
    grantLedger(convert);
    docs.grantReadWrite(convert, 'kb-source/*');
    docs.grantDelete(convert, 'kb-source/*');
    docs.grantReadWrite(convert, 'control/*');
    docs.grantDelete(convert, 'control/*');
    sync.grantInvoke(convert);
    convert.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: ['arn:aws:bedrock:*::foundation-model/*', `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`],
    }));
    convert.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:ListUsersInGroup'], resources: [userPool.userPoolArn],
    }));
    // 置かれたファイルは SQS に並べ、変換は同時に4つまでにする（まとめて置かれても Bedrock の上限にかからないように）。
    // 2回続けて途中で止まったもの（時間切れ・メモリ不足）は DLQ に移し、失敗の記録を残す
    const convertDlq = new sqs.Queue(this, 'ConvertDlq', {
      retentionPeriod: cdk.Duration.days(14), enforceSSL: true,
    });
    const convertQueue = new sqs.Queue(this, 'ConvertQueue', {
      visibilityTimeout: cdk.Duration.minutes(20), // 変換の時間切れ（15分）より長く
      retentionPeriod: cdk.Duration.days(4),
      enforceSSL: true,
      deadLetterQueue: { queue: convertDlq, maxReceiveCount: 2 },
    });
    for (const ev of [s3.EventType.OBJECT_CREATED, s3.EventType.OBJECT_REMOVED]) {
      files.addEventNotification(ev, new s3n.SqsDestination(convertQueue)); // 部署のフォルダの外は関数が無視する
    }
    // 書き起こしは、マルウェア検査が終わってから（S3 に置かれたイベントでは、置いた人を控えるだけ）
    new events.Rule(this, 'MalwareScanResult', {
      eventPattern: {
        source: ['aws.guardduty'],
        detailType: ['GuardDuty Malware Protection Object Scan Result'],
        detail: { s3ObjectDetails: { bucketName: [files.bucketName] } },
      },
      targets: [new targets.SqsQueue(convertQueue)],
    });
    convert.addEventSource(new eventsources.SqsEventSource(convertQueue, { batchSize: 1, maxConcurrency: 4 }));
    const convertFailed = this.pythonFunction('ConvertFailed', 'convert', ingestEnv, { handler: 'index.failed' });
    grantLedger(convertFailed);
    convertFailed.addEventSource(new eventsources.SqsEventSource(convertDlq, { batchSize: 10 }));

    // 大きな PDF（120ページ超）は、24ページずつに分けて並べて書き起こし、最後につなぐ（Lambda の15分に収めるため）
    const bedrockInvoke = new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: ['arn:aws:bedrock:*::foundation-model/*', `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`],
    });
    const rangeFn = this.pythonFunction('TranscribeRange', 'convert', ingestEnv, {
      handler: 'index.transcribe_range', timeout: cdk.Duration.minutes(15), memorySize: 3008,
    });
    files.grantRead(rangeFn);
    docs.grantWrite(rangeFn, 'control/*');
    rangeFn.addToRolePolicy(bedrockInvoke);
    const finishFn = this.pythonFunction('FinishLargePdf', 'convert', ingestEnv, {
      handler: 'index.finish_large_pdf', timeout: cdk.Duration.minutes(5), memorySize: 1024,
    });
    docs.grantReadWrite(finishFn, 'control/*');
    docs.grantDelete(finishFn, 'control/*');
    docs.grantReadWrite(finishFn, 'kb-source/*');
    sync.grantInvoke(finishFn);
    grantLedger(finishFn);
    finishFn.addToRolePolicy(bedrockInvoke); // タグ選び
    finishFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:ListUsersInGroup'], resources: [userPool.userPoolArn],
    }));
    const largeFailedFn = this.pythonFunction('LargePdfFailed', 'convert', ingestEnv, { handler: 'index.large_pdf_failed' });
    grantLedger(largeFailedFn);

    const recordFailure = new tasks.LambdaInvoke(this, 'RecordFailure', {
      lambdaFunction: largeFailedFn, payloadResponseOnly: true,
    }).next(new sfn.Fail(this, 'Failed'));
    const eachRange = new sfn.Map(this, 'EachRange', {
      itemsPath: sfn.JsonPath.stringAt('$.ranges'), maxConcurrency: 3, resultPath: '$.results',
    });
    eachRange.itemProcessor(new tasks.LambdaInvoke(this, 'TranscribePages', {
      lambdaFunction: rangeFn, payloadResponseOnly: true,
    }).addRetry({ errors: ['States.ALL'], maxAttempts: 1, interval: cdk.Duration.seconds(30) }));
    eachRange.addCatch(recordFailure, { resultPath: '$.error' });
    const finish = new tasks.LambdaInvoke(this, 'Finish', { lambdaFunction: finishFn, payloadResponseOnly: true });
    finish.addCatch(recordFailure, { resultPath: '$.error' });
    const largePdf = new sfn.StateMachine(this, 'LargePdf', {
      definitionBody: sfn.DefinitionBody.fromChainable(eachRange.next(finish)),
      timeout: cdk.Duration.hours(3),
    });
    largePdf.grantStartExecution(convert);
    convert.addEnvironment('LARGE_PDF_STATE_MACHINE', largePdf.stateMachineArn);

    // 既存の文書のタグを付け直す（タグの付け方を変えたときに一度流す。scripts/retag.py）
    const retag = this.pythonFunction('Retag', 'convert', ingestEnv, {
      handler: 'index.retag_all', timeout: cdk.Duration.minutes(15), memorySize: 1024,
    });
    files.grantRead(retag);
    docs.grantReadWrite(retag, 'kb-source/*');
    docs.grantReadWrite(retag, 'control/*');
    sync.grantInvoke(retag);
    grantLedger(retag);
    retag.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:ListUsersInGroup'], resources: [userPool.userPoolArn],
    }));

    // 毎日の突き合わせ（台帳・ファイル置き場・書き起こしのずれを直す。scripts/reconcile.py でも動かせる）
    const reconcile = this.pythonFunction('Reconcile', 'convert', {
      ...ingestEnv, CONVERT_QUEUE_URL: convertQueue.queueUrl,
    }, { handler: 'index.reconcile', timeout: cdk.Duration.minutes(15), memorySize: 1024 });
    files.grantRead(reconcile);
    docs.grantReadWrite(reconcile);
    docs.grantDelete(reconcile);
    grantLedger(reconcile);
    convertQueue.grantSendMessages(reconcile);
    sync.grantInvoke(reconcile);
    reconcile.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:ListUsersInGroup'], resources: [userPool.userPoolArn],
    }));
    new events.Rule(this, 'ReconcileDaily', {
      schedule: events.Schedule.cron({ minute: '0', hour: '18' }), // 毎日 3:00（日本時間）
      targets: [new targets.LambdaFunction(reconcile)],
    });

    const aclSync = this.pythonFunction('AclSync', 'acl_sync', {
      ...ingestEnv, STORAGE_ROLE_NAME: storageRole.roleName,
    }, { timeout: cdk.Duration.minutes(5) });
    docs.grantReadWrite(aclSync, 'kb-source/*');
    docs.grantReadWrite(aclSync, 'control/*');
    sync.grantInvoke(aclSync);
    grantLedger(aclSync);
    aclSync.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:ListUsersInGroup'], resources: [userPool.userPoolArn],
    }));
    // 部署から外れた人のファイル置き場の認証情報を無効にする（ロールに拒否を足す・消す）
    aclSync.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:PutRolePolicy', 'iam:DeleteRolePolicy'], resources: [storageRole.roleArn],
    }));
    // Cognito のコンソールで部署を変えても拾えるよう、定期的に比べる
    new events.Rule(this, 'AclSyncSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(aclSync)],
    });

    // ---------------- AgentCore Gateway（ツール層） ----------------
    const interceptor = this.pythonFunction('Interceptor', 'interceptor', authEnv);
    const policyEngine = new agentcore.PolicyEngine(this, 'PolicyEngine', {
      policyEngineName: `${prefix}_policy_engine`,
      description: 'Rejects Retrieve calls whose userContext does not match the JWT email',
    });
    const gateway = new agentcore.Gateway(this, 'Gateway', {
      gatewayName: `${prefix}-gateway`,
      description: 'Managed KB retrieval with per user access control',
      authorizerConfiguration: agentcore.GatewayAuthorizer.usingCognito({ userPool, allowedClients: [client] }),
      interceptorConfigurations: [agentcore.LambdaInterceptor.forRequest(interceptor, { passRequestHeaders: true })],
      policyEngineConfiguration: { policyEngine, mode: agentcore.PolicyEngineMode.ENFORCE },
    });
    gateway.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:Retrieve', 'bedrock:GetKnowledgeBase'], resources: [kb.attrKnowledgeBaseArn],
    }));
    gateway.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:AgenticRetrieveStream'], resources: ['*'], // KB に絞れない操作
    }));

    const target = new agentcore.CfnGatewayTarget(this, 'KbTarget', {
      gatewayIdentifier: gateway.gatewayId,
      name: TARGET_NAME,
      description: 'Managed knowledge base (Retrieve only)',
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      targetConfiguration: {
        mcp: {
          connector: {
            source: { connectorId: 'bedrock-knowledge-bases' },
            enabled: ['Retrieve', 'AgenticRetrieveStream'],
            configurations: [{
              // 込み入った質問向け。質問を分けて何度か検索する。答えの文章はエージェントが作るので、ここでは作らせない
              name: 'AgenticRetrieveStream',
              parameterValues: {
                retrievers: [{
                  description: '社内文書（利用者の部署の文書だけが返る）',
                  configuration: { knowledgeBase: { knowledgeBaseId: kb.attrKnowledgeBaseId } },
                }],
                agenticRetrieveConfiguration: { foundationModelType: 'MANAGED', rerankingModelType: 'MANAGED' },
                generateResponse: false,
              },
              parameterOverrides: [
                { path: '$.userContext', visible: true, description: 'システムが入れる。指定しない' },
              ],
            }, {
              name: 'Retrieve',
              parameterValues: {
                knowledgeBaseId: kb.attrKnowledgeBaseId,
                retrievalConfiguration: { managedSearchConfiguration: { numberOfResults: 8 } },
              },
              parameterOverrides: [
                { path: '$.retrievalQuery.text', visible: true, description: '検索語。日本語のまま渡す' },
                { path: '$.retrievalConfiguration.managedSearchConfiguration.filter', visible: true,
                  description: 'タグでの絞り込み。tags にはフォルダの各階層の名前と追加のタグが入る。使えるタグは指示文の一覧を見る' },
                // Interceptor が JWT から入れる。エージェントが入れた値は上書きされる
                { path: '$.userContext', visible: true, description: 'システムが入れる。指定しない' },
              ],
            }],
          },
        },
      },
    });
    target.node.addDependency(gateway.role);

    // KB を検索するツールは、userContext が JWT の email と一致するときだけ許す（それ以外は既定で拒否）
    for (const [id, tool] of [['RetrieveOwnIdentityOnly', 'Retrieve'], ['AgenticRetrieveOwnIdentityOnly', 'AgenticRetrieveStream']]) {
      new agentcore.CfnPolicy(this, id, {
        policyEngineId: policyEngine.policyEngineId,
        name: `${tool.toLowerCase()}_own_identity_only`,
        description: `Permit ${tool} only when userContext.userId equals the JWT email claim`,
        validationMode: 'FAIL_ON_ANY_FINDINGS',
        definition: {
          cedar: {
            statement: cdk.Fn.join('', [
              'permit(\n',
              '  principal is AgentCore::OAuthUser,\n',
              `  action == AgentCore::Action::"${TARGET_NAME}___${tool}",\n`,
              '  resource == AgentCore::Gateway::"', gateway.gatewayArn, '"\n',
              ') when {\n',
              '  principal.hasTag("email") &&\n',
              '  context.input has userContext &&\n',
              '  context.input.userContext has userId &&\n',
              '  context.input.userContext.userId == principal.getTag("email")\n',
              '};',
            ]),
          },
        },
      }).node.addDependency(target);
    }

    // ---------------- KB のリソースポリシー（リソース層） ----------------
    new bedrock.CfnKnowledgeBasePolicy(this, 'KbPolicy', {
      knowledgeBaseId: kb.attrKnowledgeBaseId,
      policyDocument: {
        Version: '2012-10-17',
        Statement: [{
          Sid: 'DenyRetrieveExceptGatewayRole',
          Effect: 'Deny',
          Principal: '*',
          Action: ['bedrock:Retrieve', 'bedrock:GetDocumentContent'],
          Resource: kb.attrKnowledgeBaseArn,
          Condition: { ArnNotEquals: { 'aws:PrincipalArn': gateway.role.roleArn } },
        }],
      },
    });

    // ---------------- 画面から呼ぶ API ----------------
    // チャットの途中経過と結果（1日で消える）
    const chatJobs = new dynamodb.Table(this, 'ChatJobs', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expiresAt',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const api = this.pythonFunction('Api', 'api', {
      DOCS_BUCKET: docs.bucketName, FILES_BUCKET: files.bucketName, STORAGE_ROLE_ARN: storageRole.roleArn,
      GATEWAY_URL: gateway.gatewayUrl!, SYNC_FUNCTION_NAME: sync.functionName, CHAT_JOBS_TABLE: chatJobs.tableName,
      TAGS_TABLE: tagsTable.tableName, ...ledgerEnv, ...authEnv,
    }, { timeout: cdk.Duration.minutes(5), memorySize: 1024, role: apiRole });
    tagsTable.grantReadWriteData(api);
    files.grantRead(api); // 一覧と、検索結果から元ファイルを開く署名付き URL
    chatJobs.grantReadWriteData(api);
    grantLedger(api);
    api.addToRolePolicy(new iam.PolicyStatement({ // 答えは自分自身を非同期で呼んで作る（ARN を直接書くと循環するので名前で）
      actions: ['lambda:InvokeFunction'],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${this.stackName}-Api*`],
    }));
    api.addToRolePolicy(new iam.PolicyStatement({
      // 部署は Cognito に今のグループを問い合わせて決める。タグの一括指定では、KB 用のメタデータを
      // 台帳から作り直すので、部署の人の一覧（ACL）も要る
      actions: ['cognito-idp:AdminListGroupsForUser', 'cognito-idp:ListUsersInGroup'], resources: [userPool.userPoolArn],
    }));
    docs.grantReadWrite(api, 'kb-source/*'); // 人が付け直したタグを書く
    docs.grantReadWrite(api, 'control/*');   // タグの一覧と、同期待ちの印
    sync.grantInvoke(api);
    api.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:ListBucket'], resources: [docs.bucketArn] }));
    api.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: ['arn:aws:bedrock:*::foundation-model/*', `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`],
    }));
    // API は画面と同じ CloudFront の /api/* から呼ぶ。関数 URL は IAM 認証にして、CloudFront（OAC）からしか呼べない。
    // 利用者のトークンは Authorization ではなく x-app-token で渡す（Authorization は OAC の署名が使う）
    const apiUrl = api.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.AWS_IAM });
    distribution.addBehavior('/api/*', origins.FunctionUrlOrigin.withOriginAccessControl(apiUrl, {
      readTimeout: cdk.Duration.seconds(60), // チャットは数十秒かかる。CloudFront の上限（申請なし）は60秒
    }), {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
    });
    // 関数 URL の呼び出しには InvokeFunctionUrl に加えて InvokeFunction も要る
    api.addPermission('CloudFrontInvoke', {
      principal: new iam.ServicePrincipal('cloudfront.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceArn: `arn:aws:cloudfront::${this.account}:distribution/${distribution.distributionId}`,
    });

    // ファイル置き場の CORS（画面のドメインだけ許す）。バケットのプロパティに書くと、
    // CloudFront → API の関数 → バケット → CloudFront の循環になるので、CloudFront を作ったあとに設定する
    new cr.AwsCustomResource(this, 'FilesCors', {
      onUpdate: {
        service: 'S3',
        action: 'putBucketCors',
        parameters: {
          Bucket: files.bucketName,
          CORSConfiguration: {
            CORSRules: [{
              AllowedMethods: ['GET', 'HEAD', 'PUT', 'POST', 'DELETE'],
              AllowedOrigins: [webOrigin],
              AllowedHeaders: ['*'],
              ExposeHeaders: ['last-modified', 'content-type', 'content-length', 'etag', 'x-amz-version-id',
                'x-amz-request-id', 'x-amz-id-2', 'x-amz-cf-id', 'x-amz-storage-class', 'date', 'access-control-expose-headers'],
              MaxAgeSeconds: 3000,
            }],
          },
        },
        physicalResourceId: cr.PhysicalResourceId.of(`${files.bucketName}-cors`),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([new iam.PolicyStatement({
        actions: ['s3:PutBucketCORS'], resources: [files.bucketArn],
      })]),
      installLatestAwsSdk: false,
    });

    new s3deploy.BucketDeployment(this, 'WebDeploy', {
      destinationBucket: webBucket,
      sources: [
        s3deploy.Source.asset(path.join(ROOT, 'web-app', 'dist')), // npm run build で作る
        s3deploy.Source.jsonData('config.json', {
          region: this.region, userPoolId: userPool.userPoolId, userPoolClientId: client.userPoolClientId,
          apiUrl: '', // 同じオリジンの /api/*
        }),
      ],
      distribution,
    });

    // ---------------- 操作の記録（監査） ----------------
    // アプリの操作（元ファイルを開く・質問・タグの変更・認証情報の発行・権限の無い操作の試み）は API が書く
    const auditLog = new logs.LogGroup(this, 'AuditLog', {
      logGroupName: `/${prefix}/audit`,
      retention: logs.RetentionDays.THIRTEEN_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    api.addEnvironment('AUDIT_LOG_GROUP', auditLog.logGroupName);
    api.addToRolePolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'], resources: [`${auditLog.logGroupArn}:*`],
    }));

    // AWS の操作は CloudTrail に残す。ファイル置き場の読み書き（Storage Browser の操作。セッション名がメールアドレス）と、
    // 管理の書き込み操作。記録は1年間は書き換えも削除もできないバケット（Object Lock）に置き、改ざんを検知する
    const trailBucket = new s3.Bucket(this, 'AuditTrail', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      objectLockEnabled: true,
      objectLockDefaultRetention: s3.ObjectLockRetention.governance(cdk.Duration.days(365)),
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const trail = new cloudtrail.Trail(this, 'Trail', {
      trailName: `${prefix}-audit`,
      bucket: trailBucket,
      enableFileValidation: true,
      includeGlobalServiceEvents: false,
      isMultiRegionTrail: false,
      managementEvents: cloudtrail.ReadWriteType.WRITE_ONLY,
      sendToCloudWatchLogs: true,
      cloudWatchLogsRetention: logs.RetentionDays.THIRTEEN_MONTHS,
    });
    trail.addS3EventSelector([{ bucket: files }], { readWriteType: cloudtrail.ReadWriteType.ALL });

    // 証跡があると、Cognito の部署の変更を EventBridge で受けられる。ACL をすぐ書き直す（5分おきの突き合わせは残す）
    new events.Rule(this, 'DepartmentChanged', {
      eventPattern: {
        source: ['aws.cognito-idp'],
        detailType: ['AWS API Call via CloudTrail'],
        detail: {
          eventSource: ['cognito-idp.amazonaws.com'],
          eventName: ['AdminAddUserToGroup', 'AdminRemoveUserFromGroup', 'AdminDisableUser', 'AdminEnableUser', 'AdminDeleteUser'],
          requestParameters: { userPoolId: [userPool.userPoolId] },
        },
      },
      targets: [new targets.LambdaFunction(aclSync, { event: events.RuleTargetInput.fromObject({ trigger: 'department-changed' }) })],
    });

    // ---------------- 監視と通知 ----------------
    // 気づけないまま止まる・漏れることを無くす。アラームは SNS のトピックに流す（宛先は config の alertEmail）
    const alerts = new sns.Topic(this, 'Alerts', { displayName: 'Managed KB prototype alerts' });
    if (config.alertEmail) alerts.addSubscription(new subs.EmailSubscription(config.alertEmail));
    const alarmAction = new cwActions.SnsAction(alerts);
    const alarm = (id: string, description: string, metric: cloudwatch.IMetric, threshold = 1,
      periods = 1) => {
      const a = new cloudwatch.Alarm(this, id, {
        alarmDescription: description, metric, threshold, evaluationPeriods: periods,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      a.addAlarmAction(alarmAction);
      return a;
    };
    const custom = (name: string, stat = 'Sum', minutes = 5) => new cloudwatch.Metric({
      namespace: 'ManagedKbPrototype', metricName: name, statistic: stat, period: cdk.Duration.minutes(minutes),
    });
    const fiveMin = { period: cdk.Duration.minutes(5), statistic: 'Sum' };
    for (const fn of [sync, reconcile]) {
      fn.addToRolePolicy(new iam.PolicyStatement({
        actions: ['cloudwatch:PutMetricData'], resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'ManagedKbPrototype' } },
      }));
    }

    alarm('ConvertDlqAlarm', 'Conversions stopped twice in a row and moved to the DLQ',
      convertDlq.metricApproximateNumberOfMessagesVisible({ period: cdk.Duration.minutes(5), statistic: 'Maximum' }));
    alarm('ConvertQueueAgeAlarm', 'Conversion queue has been waiting for more than 30 minutes',
      convertQueue.metricApproximateAgeOfOldestMessage({ period: cdk.Duration.minutes(5), statistic: 'Maximum' }), 1800);
    const functions: [string, lambda.Function][] = [
      ['Convert', convert], ['Api', api], ['Interceptor', interceptor], ['AclSync', aclSync], ['Reconcile', reconcile],
      ['Sync', sync], ['TranscribeRange', rangeFn], ['FinishLargePdf', finishFn], ['ConvertFailed', convertFailed],
    ];
    for (const [name, fn] of functions) {
      alarm(`${name}ErrorsAlarm`, `${name} Lambda errors`, fn.metricErrors(fiveMin));
    }
    alarm('LargePdfFailedAlarm', 'Large PDF transcription failed', largePdf.metricFailed(fiveMin));
    alarm('IngestionFailedAlarm', 'Knowledge base ingestion skipped documents', custom('IngestionFailedDocuments'));
    alarm('ReconcileGaveUpAlarm', 'Reconcile gave up on documents that never finished', custom('ReconcileGaveUp', 'Sum', 60 * 24));
    alarm('ReconcileRepairedAlarm', 'Reconcile repaired many mismatches (10 or more)', custom('ReconcileRepaired', 'Sum', 60 * 24), 10);

    // ログから数える: API が 500 を返した（例外を握って 500 にするので Lambda のエラーには出ない）、Bedrock の呼び出し上限
    const logMetric = (id: string, group: logs.ILogGroup, pattern: string, name: string) => {
      new logs.MetricFilter(this, id, {
        logGroup: group, filterPattern: logs.FilterPattern.literal(pattern),
        metricNamespace: 'ManagedKbPrototype', metricName: name, metricValue: '1',
      });
      return custom(name);
    };
    alarm('Api500Alarm', 'API returned 500', logMetric('Api500Filter', api.logGroup, '"失敗:"', 'Api500'));
    alarm('BedrockThrottleAlarm', 'Bedrock throttled conversions (5 or more in 5 minutes)',
      logMetric('ThrottleFilter', convert.logGroup, '"ThrottlingException"', 'BedrockThrottled'), 5);

    new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `${prefix}-overview`,
      widgets: [[
        new cloudwatch.GraphWidget({ title: '変換の列（待ち・DLQ）', left: [
          convertQueue.metricApproximateNumberOfMessagesVisible(), convertDlq.metricApproximateNumberOfMessagesVisible()] }),
        new cloudwatch.GraphWidget({ title: '取り込みの失敗・突き合わせ', left: [
          custom('IngestionFailedDocuments'), custom('ReconcileRepaired', 'Sum', 60), custom('DocumentsFailed', 'Maximum', 60)] }),
        new cloudwatch.GraphWidget({ title: 'Lambda のエラー', left: functions.map(([, fn]) => fn.metricErrors(fiveMin)) }),
      ], [
        new cloudwatch.GraphWidget({ title: 'API の 500・Bedrock の上限', left: [custom('Api500'), custom('BedrockThrottled')] }),
        new cloudwatch.GraphWidget({ title: '呼び出し数（変換・API）', left: [convert.metricInvocations(fiveMin), api.metricInvocations(fiveMin)] }),
        new cloudwatch.GraphWidget({ title: '大きな PDF', left: [largePdf.metricSucceeded(fiveMin), largePdf.metricFailed(fiveMin)] }),
      ]],
    });

    const out = (name: string, value: string) => new cdk.CfnOutput(this, name, { value });
    out('WebUrl', webOrigin);
    out('ApiUrl', `${webOrigin}/api`);
    out('FunctionUrl', apiUrl.url);
    out('UserPoolId', userPool.userPoolId);
    out('UserPoolClientId', client.userPoolClientId);
    out('DocsBucket', docs.bucketName);
    out('FilesBucket', files.bucketName);
    out('StorageRoleArn', storageRole.roleArn);
    out('KnowledgeBaseId', kb.attrKnowledgeBaseId);
    out('DataSourceId', dataSource.attrDataSourceId);
    out('GatewayUrl', gateway.gatewayUrl!);
    out('GatewayRoleArn', gateway.role.roleArn);
    out('AclSyncFunction', aclSync.functionName);
    out('SyncFunction', sync.functionName);
    out('ConvertLogGroup', convert.logGroup.logGroupName);
    out('LargePdfStateMachine', largePdf.stateMachineArn);
    out('RetagFunction', retag.functionName);
    out('ReconcileFunction', reconcile.functionName);
    out('DocumentsTable', documents.tableName);
    out('AlertsTopic', alerts.topicArn);
    out('AuditLogGroup', auditLog.logGroupName);
    out('TrailLogGroup', trail.logGroup!.logGroupName);
    out('ContentsTable', contents.tableName);
    out('TagsTable', tagsTable.tableName);
    out('InterceptorLogGroup', interceptor.logGroup.logGroupName);
  }

  private pythonFunction(id: string, dir: string, environment: Record<string, string>,
    extra: Partial<lambda.FunctionProps> = {}): lambda.Function {
    return new lambda.Function(this, id, {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(ROOT, 'build', dir)),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment,
      logGroup: new logs.LogGroup(this, `${id}Logs`, {
        retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      ...extra,
    });
  }
}
